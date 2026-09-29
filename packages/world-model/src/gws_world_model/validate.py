"""Semantic validation of a World Model document.

The schema already guarantees shapes and unique keys. This checks what shapes cannot: that
references resolve, that ports and domains agree, and that parameters are declared, typed and
within limits. Every issue carries the path of the offending field.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum

from gws_world_model.model import (
    Access,
    Aggregate,
    AssetCategory,
    AssetSignal,
    Direction,
    Endpoint,
    InstrumentSource,
    ParameterSpec,
    PointClass,
    Scalar,
    WorldModel,
)


class Severity(StrEnum):
    ERROR = "error"
    """The document cannot be committed."""
    WARNING = "warning"
    """Allowed, but probably not what the author meant."""


@dataclass(frozen=True, slots=True)
class Issue:
    severity: Severity
    path: str
    """Dotted path to the field, e.g. `connections[CX-12].target.port`."""
    message: str


def _type_ok(spec: ParameterSpec, value: Scalar) -> str | None:
    default = spec.default
    if isinstance(default, bool) or isinstance(value, bool):
        return (
            None
            if isinstance(default, bool) and isinstance(value, bool)
            else "expects "
            + ("a boolean" if isinstance(default, bool) else "a number or text, not a boolean")
        )
    if isinstance(default, int | float):
        if not isinstance(value, int | float):
            return "expects a number"
        if spec.min is not None and value < spec.min:
            return f"{value} is below the minimum {spec.min}"
        if spec.max is not None and value > spec.max:
            return f"{value} is above the maximum {spec.max}"
        return None
    return None if isinstance(value, str) else "expects text"


class _Validator:
    def __init__(self, doc: WorldModel) -> None:
        self.doc = doc
        self.issues: list[Issue] = []

    def error(self, path: str, message: str) -> None:
        self.issues.append(Issue(Severity.ERROR, path, message))

    def warning(self, path: str, message: str) -> None:
        self.issues.append(Issue(Severity.WARNING, path, message))

    def run(self) -> list[Issue]:
        self._site()
        self._types()
        self._assets()
        self._connections()
        self._instruments()
        self._control_bindings()
        self._point_bindings()
        self._conditions()
        return self.issues

    def _site(self) -> None:
        floors = {f.id for f in self.doc.site.floors}
        for room in self.doc.site.rooms.values():
            if room.floor not in floors:
                self.error(f"site.rooms[{room.id}].floor", f"unknown floor {room.floor!r}")
        for shaft in self.doc.site.shafts.values():
            for floor in shaft.floors:
                if floor not in floors:
                    self.error(f"site.shafts[{shaft.id}].floors", f"unknown floor {floor!r}")

    def _types(self) -> None:
        for ctype in self.doc.component_types.values():
            if ctype.parent is not None and ctype.parent not in self.doc.component_types:
                self.error(f"component_types[{ctype.id}].parent", f"unknown type {ctype.parent!r}")

    def _assets(self) -> None:
        for asset in self.doc.assets.values():
            where = f"assets[{asset.id}]"
            ctype = self.doc.component_types.get(asset.type)
            if ctype is None:
                self.error(f"{where}.type", f"unknown ComponentType {asset.type!r}")
                continue
            for name, value in asset.parameters.items():
                spec = ctype.parameters.get(name)
                if spec is None:
                    self.error(f"{where}.parameters.{name}", f"{asset.type} declares no {name!r}")
                elif (problem := _type_ok(spec, value)) is not None:
                    self.error(f"{where}.parameters.{name}", problem)
            room = asset.location.room
            if room is not None and room not in self.doc.site.rooms:
                self.error(f"{where}.location.room", f"unknown room {room!r}")

    def _endpoint(self, where: str, end: Endpoint, domain: str, upstream: bool) -> None:
        if end.is_room:
            if end.room not in self.doc.site.rooms:
                self.error(f"{where}.node", f"unknown room {end.room!r}")
            elif end.port != domain:
                self.error(f"{where}.port", f"a room's port is its domain, {domain!r}")
            return
        asset = self.doc.assets.get(end.node)
        if asset is None:
            self.error(f"{where}.node", f"unknown asset {end.node!r} (dangling connection)")
            return
        ctype = self.doc.component_types.get(asset.type)
        if ctype is None:
            return  # reported against the asset
        port = ctype.ports.get(end.port)
        if port is None:
            self.error(f"{where}.port", f"{asset.type} has no port {end.port!r}")
            return
        if port.domain != domain:
            self.error(f"{where}.port", f"port {end.port!r} carries {port.domain}, not {domain}")
        wrong = Direction.IN if upstream else Direction.OUT
        if port.direction is wrong:
            side = "source" if upstream else "target"
            self.error(
                f"{where}.port", f"port {end.port!r} faces {port.direction}; cannot be a {side}"
            )

    def _connections(self) -> None:
        seen: Counter[tuple[str, str, str]] = Counter()
        for c in self.doc.connections.values():
            where = f"connections[{c.id}]"
            self._endpoint(f"{where}.source", c.source, c.domain, upstream=True)
            self._endpoint(f"{where}.target", c.target, c.domain, upstream=False)
            if c.source == c.target:
                self.error(where, "connects a port to itself")
            seen[(c.domain, str(c.source), str(c.target))] += 1
        for (domain, source, target), n in seen.items():
            if n > 1:
                self.warning(
                    "connections", f"{n} parallel {domain} connections {source} -> {target}"
                )

    def _instruments(self) -> None:
        for i in self.doc.instruments.values():
            if i.asset not in self.doc.assets:
                self.error(f"instruments[{i.id}].asset", f"unknown asset {i.asset!r}")
            for via in i.reports_via:
                if via not in self.doc.assets:
                    self.error(f"instruments[{i.id}].reports_via", f"unknown asset {via!r}")

    def _reference(self, where: str, ref: str) -> None:
        """A `<asset>:<signal>` reference, an instrument id or a point path."""
        if ref in self.doc.instruments or ref in self.doc.point_bindings:
            return
        asset, sep, _ = ref.rpartition(":")
        if not sep or asset not in self.doc.assets:
            self.error(where, f"reference {ref!r} names no asset, instrument or point")

    def _control_bindings(self) -> None:
        for b in self.doc.control_bindings.values():
            where = f"control_bindings[{b.id}]"
            controller = self.doc.assets.get(b.controller)
            if controller is None:
                self.error(f"{where}.controller", f"unknown asset {b.controller!r}")
            else:
                ctype = self.doc.component_types.get(controller.type)
                if ctype is not None and ctype.category is not AssetCategory.CONTROLLER:
                    self.warning(f"{where}.controller", f"{controller.type} is not a controller")
            for ref in b.reads:
                self._reference(f"{where}.reads", ref)
            for ref in b.drives:
                self._reference(f"{where}.drives", ref)

    def _point_bindings(self) -> None:
        for p in self.doc.point_bindings.values():
            where = f"point_bindings[{p.path}]"
            source = p.source
            if isinstance(source, AssetSignal) and source.asset not in self.doc.assets:
                self.error(f"{where}.source.asset", f"unknown asset {source.asset!r}")
            elif (
                isinstance(source, InstrumentSource)
                and source.instrument not in self.doc.instruments
            ):
                self.error(
                    f"{where}.source.instrument", f"unknown instrument {source.instrument!r}"
                )
            elif isinstance(source, Aggregate):
                for ref in source.inputs:
                    if ref == p.path:
                        self.error(f"{where}.source.inputs", "aggregate reads itself")
                    else:
                        self._reference(f"{where}.source.inputs", ref)
            if p.access is Access.READ_WRITE and p.point_class not in (
                PointClass.COMMAND,
                PointClass.STATIC_METADATA,
                PointClass.SUPPORT,
            ):
                self.warning(f"{where}.access", f"a {p.point_class} point is writable")

    def _conditions(self) -> None:
        for room_id in self.doc.conditions.it_load:
            room = self.doc.site.rooms.get(room_id)
            if room is None:
                self.error(f"conditions.it_load[{room_id}]", "unknown room")
            elif room.kind != "hall":
                self.warning(f"conditions.it_load[{room_id}]", f"room kind is {room.kind!r}")


def validate(document: WorldModel) -> list[Issue]:
    """All issues in the document, errors and warnings, in a stable order."""
    return _Validator(document).run()


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity is Severity.ERROR]

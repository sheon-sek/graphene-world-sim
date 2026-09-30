"""Build revision 1 of the World Model from the graphene data in `data/graphene/`.

Inputs: the plant design (site, assets, connections, IT load basis) and the Ignition export
(UDT definitions plus instances). The component types come from the library; this importer
only adds their point templates from the export.

Mapping rules:

- An asset's id is its export path, or `~<name>` for equipment the export does not contain.
- A connection's id is `<domain>:<source>-><target>`. The source end uses the source type's
  first port (alphabetically) in the domain that faces out or both; the target end, the first
  that faces in or both. A room target is `room:<id>` with its domain as the port.
- Every exported point becomes a point binding with the same path, data type and typeId, so the
  Ignition contract checksum survives. A UDT member binds to its asset's signal of the same
  name. A point outside any UDT takes the source of the first rule in `graphene_bindings.json`
  whose pattern matches its whole path, or stays unbound. That file also holds the instruments
  and control bindings those sources refer to.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, TypeAdapter

from gws_world_model import library
from gws_world_model.importers.ignition import IgnitionExport, read_export
from gws_world_model.importers.point_classes import classify, in_support_folder
from gws_world_model.model import (
    ROOM_PREFIX,
    Access,
    Asset,
    AssetCategory,
    AssetSignal,
    ComponentType,
    Conditions,
    Connection,
    ControlBinding,
    Direction,
    Domain,
    Endpoint,
    Floor,
    Instrument,
    ItLoad,
    Location,
    PointBinding,
    PointClass,
    PointSource,
    PointTemplate,
    Room,
    Scalar,
    Shaft,
    Site,
    StaticValue,
    Unbound,
    WorldModel,
)

DEFINITIONS = "ignition/real-graphene-demo-udt-definitions.json"
INSTANCES = (
    "ignition/real-graphene-demo-tag-instances.json",
    "ignition/demo-twin-supplement-tag-instances.json",
)
PLANT_DESIGN = "plant-design.json"
BINDINGS = Path(__file__).with_name("graphene_bindings.json")
SUPPLEMENTS = Path(__file__).with_name("graphene_supplement")
FLOOR_HEIGHT_M = 4.5


class ImportProblem(ValueError):
    """The inputs cannot be mapped onto the World Model (listed, one per line)."""


class Rule(BaseModel):
    """Binds every loose point whose whole path matches `pattern`. Strings in `source` may use
    the pattern's groups (`\\1`, `\\g<name>`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    pattern: str
    source: dict[str, Any]
    note: str = ""


class Bindings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    instruments: list[Instrument] = []
    control_bindings: list[ControlBinding] = []
    rules: list[Rule] = []

    @classmethod
    def load(cls, path: Path = BINDINGS) -> Bindings:
        return cls.model_validate_json(path.read_bytes())


class Removal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    reason: str


class Supplement(BaseModel):
    """Engineering the asset source lacks or gets wrong, added on top of the plant design.

    Each file in `graphene_supplement/` is one reviewed change: equipment the source has no
    record of (an ATS inside a switchboard), or a correction to its schematic topology (an
    isolation valve drawn in parallel with the pump it is in series with). Every entry says
    why in `note` or `reason`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    note: str
    assets: list[Asset] = []
    asset_parameters: dict[str, dict[str, Scalar]] = {}
    """Parameter values for assets the plant design has, by asset id (merged over theirs)."""
    remove_connections: list[Removal] = []
    connections: list[Connection] = []
    instruments: list[Instrument] = []
    control_bindings: list[ControlBinding] = []
    member_units: dict[str, dict[str, str]] = {}
    """Units the Ignition export leaves off a UDT's members, by type id, then member path. A
    unit is only filled in where the export has none."""

    @classmethod
    def load_all(cls, directory: Path = SUPPLEMENTS) -> list[Supplement]:
        return [cls.model_validate_json(p.read_bytes()) for p in sorted(directory.glob("*.json"))]


_SOURCE: TypeAdapter[PointSource] = TypeAdapter(PointSource)


def _expand(value: Any, match: re.Match[str]) -> Any:
    if isinstance(value, str):
        return match.expand(value)
    if isinstance(value, list):
        return [_expand(v, match) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v, match) for k, v in value.items()}
    return value


class _Binder:
    def __init__(self, rules: list[Rule]) -> None:
        self.rules = [(re.compile(r.pattern), r) for r in rules]

    def source(self, path: str) -> PointSource | None:
        for pattern, rule in self.rules:
            if (match := pattern.fullmatch(path)) is not None:
                return _SOURCE.validate_python(_expand(rule.source, match))
        return None


@dataclass(frozen=True, slots=True)
class Sources:
    design: dict[str, Any]
    export: IgnitionExport

    @classmethod
    def read(cls, data_dir: Path) -> Sources:
        design = json.loads((data_dir / PLANT_DESIGN).read_text(encoding="utf-8"))
        export = read_export(data_dir / DEFINITIONS, [data_dir / p for p in INSTANCES])
        return cls(design, export)


def contract_checksum(doc: WorldModel) -> str:
    """The Ignition contract checksum computed from the point bindings of a document."""
    lines = (
        f"{p.path}\t{p.data_type}\t{p.ignition_type_id or ''}"
        for p in sorted(doc.point_bindings.values(), key=lambda p: p.path)
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _access(point_class: PointClass) -> Access:
    return Access.READ_WRITE if point_class is PointClass.COMMAND else Access.READ


def _templates(
    ctype: ComponentType, export: IgnitionExport, fill: dict[str, str], problems: list[str]
) -> dict[str, PointTemplate]:
    udt = export.types.get(ctype.ignition_type_id or "")
    if udt is None:
        return {}
    for member in fill.keys() - udt.members.keys():
        problems.append(f"supplement sets the unit of {ctype.id} member {member!r}, which it lacks")
    templates = {}
    for member, tag in udt.members.items():
        data_type = tag.get("dataType") or "Int4"
        if tag.get("engUnit") and member in fill:
            problems.append(
                f"supplement sets the unit of {ctype.id} member {member!r}, which has one"
            )
        unit = tag.get("engUnit") or fill.get(member) or None
        point_class = classify(
            path=member,
            name=member.rsplit("/", 1)[-1],
            type_id=ctype.id,
            data_type=data_type,
            value_source=tag.get("valueSource") or "memory",
            unit=unit,
            support=ctype.category is AssetCategory.SUPPORT,
        )
        templates[member] = PointTemplate(
            data_type=data_type,
            unit=unit,
            point_class=point_class,
            access=_access(point_class),
            signal=member,
        )
    return templates


def _site(design: dict[str, Any]) -> Site:
    floors = tuple(
        Floor(id=name, index=i, elevation_m=i * FLOOR_HEIGHT_M, height_m=FLOOR_HEIGHT_M)
        for i, name in enumerate(design["floors"])
    )
    rooms = {
        r["id"]: Room(
            id=r["id"],
            floor=r["floor"],
            name=r["name"],
            kind=r["kind"],
            x=r["x"],
            y=r["y"],
            w=r["w"],
            h=r["h"],
            fire_zone=r.get("fire"),
            outdoor=r.get("outdoor", False),
        )
        for r in design["rooms"]
    }
    shafts = {
        s["id"]: Shaft(
            id=s["id"],
            name=s["name"],
            x=s["x"],
            y=s["y"],
            floors=tuple(s["floors"]),
            carries=tuple(Domain(c) for c in s["carries"]),
        )
        for s in design["shafts"]
    }
    return Site(
        id="graphene", name="Graphene data centre", floors=floors, rooms=rooms, shafts=shafts
    )


def _assets(design: dict[str, Any]) -> dict[str, Asset]:
    names = {u["id"]: u["name"] for u in design["unexported"]}
    assets = {}
    for a in design["assets"]:
        path = a["path"]
        assets[path] = Asset(
            id=path,
            type=a["type"],
            name=names.get(path, path.rsplit("/", 1)[-1].removeprefix("~")),
            location=Location(room=a["room"], x=a["x"], y=a["y"]),
            system=a["sys"],
            role=a["role"],
            exported=not a["unexported"],
        )
    return assets


def _port(ctype: ComponentType, domain: Domain, upstream: bool) -> str | None:
    faces = (Direction.OUT, Direction.BOTH) if upstream else (Direction.IN, Direction.BOTH)
    names = sorted(n for n, p in ctype.ports.items() if p.domain is domain and p.direction in faces)
    return names[0] if names else None


def _connections(
    design: dict[str, Any],
    assets: dict[str, Asset],
    types: dict[str, ComponentType],
    rooms: dict[str, Room],
    problems: list[str],
) -> dict[str, Connection]:
    connections = {}
    for edge in design["edges"]:
        domain = Domain(edge["kind"])
        ends = []
        for node, upstream in ((edge["a"], True), (edge["b"], False)):
            if not upstream and node in rooms:
                ends.append(Endpoint(node=f"{ROOM_PREFIX}{node}", port=domain.value))
                continue
            ctype = types[assets[node].type]
            port = _port(ctype, domain, upstream)
            if port is None:
                side = "out" if upstream else "in"
                problems.append(f"{ctype.id} has no {domain} port facing {side} (for {node})")
                port = f"?{domain}"
            ends.append(Endpoint(node=node, port=port))
        cid = f"{domain}:{edge['a']}->{edge['b']}"
        connections[cid] = Connection(
            id=cid, domain=domain, source=ends[0], target=ends[1], label=edge.get("label", "")
        )
    return connections


SUPPORT_DEFAULTS: dict[str, Scalar] = {
    "Boolean": False,
    "Float4": 0.0,
    "Float8": 0.0,
    "String": "",
    "DataSet": "{}",
    "Document": "{}",
}
"""The constant a support point reads, by data type (0 for integers and DateTimes)."""


def _point_bindings(
    export: IgnitionExport,
    assets: dict[str, Asset],
    types: dict[str, ComponentType],
    binder: _Binder,
    member_units: dict[str, dict[str, str]],
) -> dict[str, PointBinding]:
    bindings = {}
    for p in export.points.values():
        support = in_support_folder(p.path)
        unit = p.eng_unit
        if p.asset is not None:
            support = support or types[assets[p.asset].type].category is AssetCategory.SUPPORT
            if unit is None and p.member is not None:
                unit = member_units.get(assets[p.asset].type, {}).get(p.member)
        point_class = classify(
            path=p.path,
            name=p.name,
            type_id=p.type_id,
            data_type=p.data_type,
            value_source=p.value_source,
            unit=unit,
            support=support,
        )
        source: PointSource | None
        if point_class is PointClass.SUPPORT:
            # An Ignition-side artefact with no physical counterpart: served as a constant,
            # outside the simulation and its physics coverage.
            source = StaticValue(value=SUPPORT_DEFAULTS.get(p.data_type, 0))
        elif p.asset is not None and p.member is not None:
            # A rule wins over the member only for what the asset cannot compute itself: an
            # identifier, or a widget that shows other points.
            ruled = point_class is PointClass.STATIC_METADATA or (
                types[assets[p.asset].type].category is AssetCategory.AGGREGATE
            )
            source = (binder.source(p.path) if ruled else None) or AssetSignal(
                asset=p.asset, signal=p.member
            )
        elif (source := binder.source(p.path)) is not None:
            pass
        else:
            source = Unbound(reason="point outside any UDT; not bound yet")
        if isinstance(source, StaticValue) and isinstance(source.value, str):
            # A rule's text (a name captured from the path) in a numeric point is its number.
            if p.data_type.startswith("Int") and source.value.isdigit():
                source = StaticValue(value=int(source.value))
        bindings[p.path] = PointBinding(
            path=p.path,
            data_type=p.data_type,
            unit=unit,
            point_class=point_class,
            access=_access(point_class),
            ignition_type_id=p.type_id,
            source=source,
        )
    return bindings


def _conditions(design: dict[str, Any]) -> Conditions:
    it_load = {
        hall["hall"]: ItLoad(
            design_kw=hall["design_kW"],
            fraction=round(sum(hall["operating_pct"]) / len(hall["operating_pct"]) / 100, 6),
            liquid_fraction=hall["liquid_fraction"],
        )
        for hall in design["basis"]["it"]
    }
    return Conditions(it_load=it_load)


def _add[T: (Asset, Connection, Instrument, ControlBinding)](
    kind: str, target: dict[str, T], items: list[T], problems: list[str]
) -> None:
    for item in items:
        if item.id in target:
            problems.append(f"supplement adds {kind} {item.id} twice")
        target[item.id] = item


def _supplement(
    supplements: list[Supplement],
    assets: dict[str, Asset],
    connections: dict[str, Connection],
    problems: list[str],
) -> tuple[dict[str, Instrument], dict[str, ControlBinding]]:
    instruments: dict[str, Instrument] = {}
    control_bindings: dict[str, ControlBinding] = {}
    for sup in supplements:
        for removal in sup.remove_connections:
            if connections.pop(removal.id, None) is None:
                problems.append(f"supplement removes unknown connection {removal.id}")
        _add("asset", assets, sup.assets, problems)
        for asset_id, values in sup.asset_parameters.items():
            if asset_id not in assets:
                problems.append(f"supplement sets parameters of unknown asset {asset_id}")
                continue
            merged = {**assets[asset_id].parameters, **values}
            assets[asset_id] = assets[asset_id].model_copy(update={"parameters": merged})
        _add("connection", connections, sup.connections, problems)
        _add("instrument", instruments, sup.instruments, problems)
        _add("control binding", control_bindings, sup.control_bindings, problems)
    return instruments, control_bindings


def build(
    sources: Sources,
    types: dict[str, ComponentType] | None = None,
    bindings: Bindings | None = None,
    supplements: list[Supplement] | None = None,
) -> WorldModel:
    """The World Model the graphene data describes. Raises ImportProblem listing every gap."""
    library_types = library.load() if types is None else types
    extra = Bindings.load() if bindings is None else bindings
    added = Supplement.load_all() if supplements is None else supplements
    problems: list[str] = []
    member_units: dict[str, dict[str, str]] = {}
    for sup in added:
        for type_id, units in sup.member_units.items():
            if type_id not in library_types:
                problems.append(f"supplement sets member units of unknown type {type_id!r}")
            member_units.setdefault(type_id, {}).update(units)
    component_types = {
        t.id: t.model_copy(
            update={
                "point_template": _templates(
                    t, sources.export, member_units.get(t.id, {}), problems
                )
            }
        )
        for t in library_types.values()
    }
    assets = _assets(sources.design)
    for asset in assets.values():
        if asset.type not in component_types:
            problems.append(f"no library type {asset.type!r} (for {asset.id})")
    for path in sources.export.instances:
        if path not in assets:
            problems.append(f"exported instance {path} is not in the plant design")
    if problems:
        raise ImportProblem("\n".join(sorted(set(problems))))
    site = _site(sources.design)
    connections = _connections(sources.design, assets, component_types, site.rooms, problems)
    instruments, control_bindings = _supplement(added, assets, connections, problems)
    for key in instruments.keys() & {i.id for i in extra.instruments}:
        problems.append(f"instrument {key} is defined in the bindings and a supplement")
    for key in control_bindings.keys() & {b.id for b in extra.control_bindings}:
        problems.append(f"control binding {key} is defined in the bindings and a supplement")
    if problems:
        raise ImportProblem("\n".join(sorted(set(problems))))
    return WorldModel(
        site=site,
        component_types=component_types,
        assets=assets,
        connections=connections,
        instruments={i.id: i for i in extra.instruments} | instruments,
        control_bindings={b.id: b for b in extra.control_bindings} | control_bindings,
        point_bindings=_point_bindings(
            sources.export, assets, component_types, _Binder(extra.rules), member_units
        ),
        conditions=_conditions(sources.design),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("data_dir", type=Path, help="the data/graphene directory")
    parser.add_argument("output", type=Path, help="where to write the World Model JSON")
    args = parser.parse_args(argv)
    doc = build(Sources.read(args.data_dir))
    args.output.write_text(doc.canonical_json() + "\n", encoding="utf-8")
    print(f"{len(doc.assets)} assets, {len(doc.connections)} connections, ", end="")
    print(f"{len(doc.point_bindings)} points; contract {contract_checksum(doc)}")


if __name__ == "__main__":
    main()

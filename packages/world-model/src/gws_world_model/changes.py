"""Differences between two World Model documents, each with its Change Class.

The class says how the edit reaches a running simulation (CONTEXT.md, Change Class). The rules:

| What changed | Class |
| --- | --- |
| An asset added or removed, or its type changed | structural |
| An asset parameter | the class its ParameterSpec declares (structural if undeclared) |
| An asset's name, location, system, role | live |
| A connection added, removed or re-routed | structural |
| A connection parameter (length, impedance) | warm |
| A control binding (compiled into the plant model) | structural |
| An instrument or a point binding | live |
| A ComponentType's behaviour, parameters, ports or fault modes | reinitialise |
| A ComponentType's point template or description | live |
| Operating conditions | live |
| Room geometry or floor heights (they size air volumes) | warm |
| Other site layout | live |
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from gws_world_model.model import COLLECTIONS, ChangeClass, WorldModel


class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    MODIFIED = "modified"


@dataclass(frozen=True, slots=True)
class Change:
    collection: str
    """A WorldModel collection, or `site` / `conditions`."""
    key: str
    kind: ChangeKind
    fields: tuple[str, ...]
    """Dotted paths of the fields that differ (empty when added or removed)."""
    change_class: ChangeClass
    reason: str


def _leaf_diff(a: Any, b: Any, prefix: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b), key=str):
            path = f"{prefix}.{key}" if prefix else str(key)
            if key not in a or key not in b:
                out.append(path)
            else:
                out.extend(_leaf_diff(a[key], b[key], path))
        return out
    return [] if a == b else [prefix or "."]


def _max(classes: list[tuple[ChangeClass, str]]) -> tuple[ChangeClass, str]:
    return max(classes, key=lambda c: c[0].rank)


class _Classifier:
    def __init__(self, old: WorldModel, new: WorldModel) -> None:
        self.old, self.new = old, new

    def entity(self, collection: str, key: str, kind: ChangeKind, fields: list[str]) -> Change:
        if kind is not ChangeKind.MODIFIED:
            cls, reason = self._added_or_removed(collection, kind)
        else:
            cls, reason = _max([self._field(collection, key, f) for f in fields])
        return Change(collection, key, kind, tuple(fields), cls, reason)

    def _added_or_removed(self, collection: str, kind: ChangeKind) -> tuple[ChangeClass, str]:
        if collection in ("instruments", "point_bindings"):
            return ChangeClass.LIVE, f"{collection} are outside the plant model"
        if collection == "component_types":
            return ChangeClass.LIVE, f"type {kind}; assets that use it are classified separately"
        return ChangeClass.STRUCTURAL, f"{collection.removesuffix('s')} {kind}"

    def _field(self, collection: str, key: str, field: str) -> tuple[ChangeClass, str]:
        head = field.split(".", 1)[0]
        if collection == "assets":
            if head == "type":
                return ChangeClass.STRUCTURAL, "asset type changed"
            if head == "parameters":
                return self._asset_parameter(key, field.split(".", 2)[1])
            return ChangeClass.LIVE, f"{head} does not affect behaviour"
        if collection == "connections":
            if head == "parameters":
                return ChangeClass.WARM, "connection parameter"
            if head == "label":
                return ChangeClass.LIVE, "label"
            return ChangeClass.STRUCTURAL, "connection re-routed"
        if collection == "control_bindings":
            return ChangeClass.STRUCTURAL, "control wiring is compiled into the plant model"
        if collection == "component_types":
            if head in ("point_template", "description", "name"):
                return ChangeClass.LIVE, f"type {head}"
            return ChangeClass.REINITIALISE, f"type {head} changes every instance's behaviour"
        return ChangeClass.LIVE, f"{collection} are outside the plant model"

    def _asset_parameter(self, key: str, name: str) -> tuple[ChangeClass, str]:
        asset = self.new.assets.get(key) or self.old.assets[key]
        ctype = self.new.component_types.get(asset.type)
        spec = None if ctype is None else ctype.parameters.get(name)
        if spec is None:
            return ChangeClass.STRUCTURAL, f"parameter {name} is not declared by {asset.type}"
        return spec.change_class, f"parameter {name} is declared {spec.change_class}"

    def site(self, fields: list[str]) -> Change:
        warm = [f for f in fields if f.split(".")[-1] in ("w", "h", "height_m", "elevation_m")]
        if warm:
            return Change(
                "site",
                "site",
                ChangeKind.MODIFIED,
                tuple(fields),
                ChangeClass.WARM,
                "room or floor geometry sizes air volumes",
            )
        return Change(
            "site", "site", ChangeKind.MODIFIED, tuple(fields), ChangeClass.LIVE, "layout only"
        )


def diff(old: WorldModel, new: WorldModel) -> list[Change]:
    """Every entity that differs between two documents, in collection then key order."""
    classify = _Classifier(old, new)
    changes: list[Change] = []
    for collection in COLLECTIONS:
        a: dict[str, Any] = getattr(old, collection)
        b: dict[str, Any] = getattr(new, collection)
        for key in sorted(set(a) | set(b)):
            if key not in b:
                changes.append(classify.entity(collection, key, ChangeKind.REMOVED, []))
            elif key not in a:
                changes.append(classify.entity(collection, key, ChangeKind.ADDED, []))
            elif a[key] != b[key]:
                fields = _leaf_diff(a[key].model_dump(mode="json"), b[key].model_dump(mode="json"))
                changes.append(classify.entity(collection, key, ChangeKind.MODIFIED, fields))
    if old.site != new.site:
        changes.append(
            classify.site(
                _leaf_diff(old.site.model_dump(mode="json"), new.site.model_dump(mode="json"))
            )
        )
    if old.conditions != new.conditions:
        fields = _leaf_diff(
            old.conditions.model_dump(mode="json"), new.conditions.model_dump(mode="json")
        )
        changes.append(
            Change(
                "conditions",
                "conditions",
                ChangeKind.MODIFIED,
                tuple(fields),
                ChangeClass.LIVE,
                "operating conditions apply at the next step",
            )
        )
    return changes


def overall(changes: list[Change]) -> ChangeClass | None:
    """The most disruptive class in a diff; None when nothing changed."""
    return max((c.change_class for c in changes), key=lambda c: c.rank, default=None)

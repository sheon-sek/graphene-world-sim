"""Read an Ignition tag export (UDT definitions + tag instances) into flat point records.

Ignition semantics this follows:

- A UDT type may extend a parent type and inherits its members.
- A UDT type may contain nested UDT instances; their members become members of the outer type
  under the nested instance's path.
- A property of a point resolves along a chain, most specific last: the member in the base
  type, the member in the derived type, the override a nested instance carries inside its
  enclosing type, then the override on the exported instance. A property absent from the whole
  chain is at Ignition's default, which the export omits.
- The root UDT instance is the Asset; every point beneath it belongs to that Asset.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

type Tag = dict[str, Any]

DEFAULT_DATA_TYPE = "Int4"
DEFAULT_VALUE_SOURCE = "memory"


@dataclass(frozen=True, slots=True)
class UdtType:
    type_id: str
    """typeId including its folder (`Production/GPM96`)."""
    parent: str | None
    members: dict[str, Tag]
    """Member path to its resolved definition (base first, overrides applied)."""


@dataclass(frozen=True, slots=True)
class ExportedInstance:
    path: str
    type_id: str


@dataclass(frozen=True, slots=True)
class ExportedPoint:
    path: str
    name: str
    data_type: str
    value_source: str
    eng_unit: str | None
    value: Any
    member: str | None
    """Path relative to its Asset; None for a point outside any UDT instance."""
    asset: str | None
    """Export path of the root UDT instance, if any."""
    type_id: str | None
    """typeId of the nearest enclosing UDT instance."""


@dataclass
class IgnitionExport:
    types: dict[str, UdtType]
    instances: dict[str, ExportedInstance]
    """Root UDT instances only (the Assets), by export path."""
    points: dict[str, ExportedPoint]

    def contract_checksum(self) -> str:
        """SHA-256 of the sorted `path<TAB>dataType<TAB>typeId` lines: the Ignition contract."""
        lines = (
            f"{p.path}\t{p.data_type}\t{p.type_id or ''}"
            for p in sorted(self.points.values(), key=lambda p: p.path)
        )
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _join(prefix: str, name: str) -> str:
    return f"{prefix}/{name}" if prefix else name


def _resolve(chain: list[Tag], prop: str) -> Any:
    for definition in reversed(chain):
        if prop in definition:
            return definition[prop]
    return None


@dataclass
class _Flat:
    members: dict[str, list[Tag]] = field(default_factory=dict)
    nested: dict[str, str] = field(default_factory=dict)


class _Types:
    def __init__(self, definitions: Tag) -> None:
        self.raw: dict[str, Tag] = {}
        self._flat: dict[str, _Flat] = {}
        self._collect(definitions, "")

    def _collect(self, node: Tag, prefix: str) -> None:
        for child in node.get("tags", []):
            type_id = _join(prefix, child["name"])
            if child["tagType"] == "UdtType":
                self.raw[type_id] = child
            elif child["tagType"] == "Folder":
                self._collect(child, type_id)
            else:
                raise ValueError(f"unexpected {child['tagType']} among UDT types: {type_id}")

    def flat(self, type_id: str) -> _Flat:
        if type_id in self._flat:
            return self._flat[type_id]
        if type_id not in self.raw:
            raise ValueError(f"UDT type not in the export: {type_id!r}")
        raw = self.raw[type_id]
        flat = self._flat[type_id] = _Flat()
        if parent := raw.get("typeId"):
            base = self.flat(parent)
            flat.members = {k: list(v) for k, v in base.members.items()}
            flat.nested = dict(base.nested)
        self._flatten(raw, "", flat)
        return flat

    def _flatten(self, node: Tag, rel: str, flat: _Flat) -> None:
        for child in node.get("tags", []):
            key = _join(rel, child["name"])
            kind = child["tagType"]
            if kind == "AtomicTag":
                flat.members.setdefault(key, []).append(child)
            elif kind == "UdtInstance":
                if type_id := child.get("typeId"):
                    inner = self.flat(type_id)
                    for member, chain in inner.members.items():
                        flat.members.setdefault(f"{key}/{member}", []).extend(chain)
                    for member, nested_type in inner.nested.items():
                        flat.nested[f"{key}/{member}"] = nested_type
                    flat.nested[key] = type_id
                self._flatten(child, key, flat)
            elif kind == "Folder":
                self._flatten(child, key, flat)
            else:
                raise ValueError(f"unexpected {kind} inside a UDT type: {key}")

    def udt_type(self, type_id: str) -> UdtType:
        flat = self.flat(type_id)
        members = {}
        for member, chain in sorted(flat.members.items()):
            resolved: Tag = {}
            for definition in chain:
                resolved.update(definition)
            members[member] = resolved
        return UdtType(type_id, self.raw[type_id].get("typeId") or None, members)


@dataclass(frozen=True)
class _Owner:
    asset: str
    flat: _Flat
    type_id: str


class _Walker:
    def __init__(self, types: _Types) -> None:
        self.types = types
        self.instances: dict[str, ExportedInstance] = {}
        self.points: dict[str, ExportedPoint] = {}

    def walk(self, node: Tag, prefix: str, owner: _Owner | None) -> None:
        for child in node.get("tags", []):
            path = _join(prefix, child["name"])
            kind = child["tagType"]
            if kind == "AtomicTag":
                self._point(child, path, owner)
            elif kind == "UdtInstance":
                self.walk(child, path, self._enter(child, path, owner))
            elif kind == "Folder":
                self.walk(child, path, owner)
            else:
                raise ValueError(f"unexpected {kind} in the instance export: {path}")

    def _enter(self, tag: Tag, path: str, outer: _Owner | None) -> _Owner:
        if outer is None:
            type_id = tag["typeId"]
            if path in self.instances:
                raise ValueError(f"duplicate UDT instance: {path}")
            self.instances[path] = ExportedInstance(path, type_id)
            return _Owner(path, self.types.flat(type_id), type_id)
        rel = path.removeprefix(f"{outer.asset}/")
        type_id = tag.get("typeId") or outer.flat.nested.get(rel)
        if type_id is None:
            raise ValueError(f"nested UDT instance not in its type: {path}")
        return _Owner(outer.asset, outer.flat, type_id)

    def _point(self, tag: Tag, path: str, owner: _Owner | None) -> None:
        if path in self.points:
            raise ValueError(f"duplicate point: {path}")
        if owner is None:
            member = None
            chain = [tag]
        else:
            member = path.removeprefix(f"{owner.asset}/")
            if member not in owner.flat.members:
                raise ValueError(f"point is not a member of UDT type {owner.type_id!r}: {path}")
            chain = [*owner.flat.members[member], tag]
        self.points[path] = ExportedPoint(
            path=path,
            name=tag["name"],
            data_type=_resolve(chain, "dataType") or DEFAULT_DATA_TYPE,
            value_source=_resolve(chain, "valueSource") or DEFAULT_VALUE_SOURCE,
            eng_unit=_resolve(chain, "engUnit") or None,
            value=_resolve(chain, "value"),
            member=member,
            asset=None if owner is None else owner.asset,
            type_id=None if owner is None else owner.type_id,
        )


def read_export(definitions: Path, instances: Iterable[Path]) -> IgnitionExport:
    """Parse UDT definitions plus one or more instance exports (later files add tags)."""
    types = _Types(json.loads(definitions.read_text(encoding="utf-8")))
    walker = _Walker(types)
    for path in instances:
        walker.walk(json.loads(path.read_text(encoding="utf-8")), "", None)
    return IgnitionExport(
        types={t: types.udt_type(t) for t in sorted(types.raw)},
        instances=walker.instances,
        points=walker.points,
    )

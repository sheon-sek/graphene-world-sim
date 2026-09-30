"""Draft operations: the edits a Draft holds on top of its base revision."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from gws_world_model.model import COLLECTIONS, Conditions, Site, WorldModel

type Collection = Literal[
    "component_types",
    "assets",
    "connections",
    "instruments",
    "control_bindings",
    "point_bindings",
]


class _Op(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Put(_Op):
    """Add an entity, or replace it whole. Its key is its `id` (`path` for point bindings)."""

    op: Literal["put"] = "put"
    collection: Collection
    value: dict[str, Any]


class Delete(_Op):
    op: Literal["delete"] = "delete"
    collection: Collection
    key: str


class Place(_Op):
    """Add an asset. An exported asset also gets a point binding for every member of its
    type's point template, at `<asset id>/<member>`, reading the member's signal."""

    op: Literal["place"] = "place"
    value: dict[str, Any]


class Remove(_Op):
    """Remove an asset and everything that only exists through it: its connections,
    instruments and point bindings, and the controller bindings it runs. References to it
    elsewhere (a controller reading it, an aggregate summing it, an instrument reporting
    through it) are dropped from those entities."""

    op: Literal["remove"] = "remove"
    key: str


class SetConditions(_Op):
    op: Literal["set_conditions"] = "set_conditions"
    value: Conditions


class SetSite(_Op):
    op: Literal["set_site"] = "set_site"
    value: Site


type Operation = Annotated[
    Put | Delete | Place | Remove | SetConditions | SetSite, Field(discriminator="op")
]

OPERATIONS: TypeAdapter[list[Operation]] = TypeAdapter(list[Operation])


class OperationError(ValueError):
    """An operation cannot be applied to the document (unknown key, malformed entity)."""


def _key_field(collection: str) -> str:
    return "path" if collection == "point_bindings" else "id"


def _plain(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return dict(value)


def _place(data: dict[str, Any], asset: dict[str, Any], where: str) -> None:
    key = asset.get("id")
    if not isinstance(key, str) or not key:
        raise OperationError(f"{where}: value has no id")
    if key in data["assets"]:
        raise OperationError(f"{where}: assets[{key!r}] already exists")
    ctype = data["component_types"].get(asset.get("type"))
    if ctype is None:
        raise OperationError(f"{where}: unknown component type {asset.get('type')!r}")
    ctype = _plain(ctype)
    data["assets"][key] = asset
    if not asset.get("exported", True):
        return
    for member, template in ctype.get("point_template", {}).items():
        path = f"{key}/{member}"
        data["point_bindings"][path] = {
            "path": path,
            "data_type": template["data_type"],
            "unit": template.get("unit"),
            "point_class": template["point_class"],
            "access": template.get("access", "read"),
            "ignition_type_id": ctype["id"],
            "source": {
                "kind": "asset_signal",
                "asset": key,
                "signal": template.get("signal") or member,
            },
        }


def _remove(data: dict[str, Any], key: str, where: str) -> None:
    if key not in data["assets"]:
        raise OperationError(f"{where}: assets[{key!r}] does not exist")
    del data["assets"][key]
    for cid, c in list(data["connections"].items()):
        c = _plain(c)
        if key in (c["source"]["node"], c["target"]["node"]):
            del data["connections"][cid]
    gone: set[str] = set()
    for iid, i in list(data["instruments"].items()):
        i = _plain(i)
        if i["asset"] == key:
            gone.add(iid)
            del data["instruments"][iid]
        elif key in i.get("reports_via", ()):
            i["reports_via"] = [x for x in i["reports_via"] if x != key]
            data["instruments"][iid] = i

    removed: set[str] = set()
    for path, b in list(data["point_bindings"].items()):
        source = _plain(b)["source"]
        if (
            path.startswith(f"{key}/")
            or source.get("asset") == key
            or source.get("instrument") in gone
        ):
            removed.add(path)
            del data["point_bindings"][path]

    def refers(reference: str) -> bool:
        return reference in gone or reference in removed or reference.split(":", 1)[0] == key

    for cid, b in list(data["control_bindings"].items()):
        b = _plain(b)
        if b["controller"] == key:
            del data["control_bindings"][cid]
            continue
        reads = [r for r in b.get("reads", ()) if not refers(r)]
        drives = [r for r in b.get("drives", ()) if not refers(r)]
        if (reads, drives) != (list(b.get("reads", ())), list(b.get("drives", ()))):
            data["control_bindings"][cid] = b | {"reads": reads, "drives": drives}
    for path, b in list(data["point_bindings"].items()):
        plain = _plain(b)
        source = plain["source"]
        if source.get("kind") == "aggregate":
            inputs = [r for r in source["inputs"] if not refers(r)]
            if inputs != list(source["inputs"]):
                data["point_bindings"][path] = plain | {"source": source | {"inputs": inputs}}


def apply(document: WorldModel, operations: list[Operation]) -> WorldModel:
    """Return a new document with the operations applied in order. The input is not changed."""
    data: dict[str, Any] = {name: dict(getattr(document, name)) for name in COLLECTIONS}
    site, conditions = document.site, document.conditions
    for index, op in enumerate(operations):
        where = f"operation {index}"
        if isinstance(op, Put):
            key = op.value.get(_key_field(op.collection))
            if not isinstance(key, str) or not key:
                raise OperationError(f"{where}: value has no {_key_field(op.collection)}")
            data[op.collection][key] = op.value
        elif isinstance(op, Delete):
            if op.key not in data[op.collection]:
                raise OperationError(f"{where}: {op.collection}[{op.key!r}] does not exist")
            del data[op.collection][op.key]
        elif isinstance(op, Place):
            _place(data, dict(op.value), where)
        elif isinstance(op, Remove):
            _remove(data, op.key, where)
        elif isinstance(op, SetConditions):
            conditions = op.value
        else:
            site = op.value
    try:
        return WorldModel.model_validate(
            {
                "schema_version": document.schema_version,
                "site": site,
                "conditions": conditions,
                **data,
            }
        )
    except ValueError as exc:
        raise OperationError(str(exc)) from exc

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


class SetConditions(_Op):
    op: Literal["set_conditions"] = "set_conditions"
    value: Conditions


class SetSite(_Op):
    op: Literal["set_site"] = "set_site"
    value: Site


type Operation = Annotated[Put | Delete | SetConditions | SetSite, Field(discriminator="op")]

OPERATIONS: TypeAdapter[list[Operation]] = TypeAdapter(list[Operation])


class OperationError(ValueError):
    """An operation cannot be applied to the document (unknown key, malformed entity)."""


def _key_field(collection: str) -> str:
    return "path" if collection == "point_bindings" else "id"


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

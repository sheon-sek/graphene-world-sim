"""The interface the serving process uses to hand points to the OPC UA server (ADR-0001).

The server knows nothing about the World Model or the simulator. It is told which points exist
(`PointSpec`), receives their current values (`PointValue`), and hands every write to a
command point back through a `WriteHandler`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

Scalar = bool | int | float | str | datetime
"""A value a point carries on the wire."""


@dataclass(frozen=True, slots=True)
class PointSpec:
    """One point of the address space."""

    path: str
    """Export path: folders from `Objects`, then the point's BrowseName."""
    data_type: str
    """Ignition data type: Float4, Float8, Int4, Int8, Boolean, String, DateTime, DataSet or
    Document."""
    writable: bool = False
    """A command point: clients may write it, and each write goes to the WriteHandler."""
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class PointValue:
    """A point's current value, as the instrumentation layer measured it."""

    value: Any
    quality: str = "good"
    """`good`, `uncertain` or `bad`."""
    timestamp: datetime | None = None
    """When the value was produced (the simulation time of its step)."""
    reason: str = ""
    """Why the quality is not good: `comm_lost`, `sensor_failed`, `out_of_range`, `unbound`,
    `not_simulated` or `out_of_scope`. It selects the specific OPC UA status code."""


WriteHandler = Callable[[str, Scalar], Awaitable[str | None]]
"""Called with a command point's path and the written value. Returns None when the command is
accepted, or the reason it was rejected."""

DATA_TYPES = frozenset(
    {"Float4", "Float8", "Int4", "Int8", "Boolean", "String", "DateTime", "DataSet", "Document"}
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_INT_RANGES = {"Int4": (-(2**31), 2**31 - 1), "Int8": (-(2**63), 2**63 - 1)}


class Mismatch(ValueError):
    """A value that cannot be carried in its point's data type."""


def default(data_type: str) -> Scalar:
    """The value a point shows before it has one."""
    if data_type.startswith("Float"):
        return 0.0
    if data_type.startswith("Int"):
        return 0
    if data_type == "Boolean":
        return False
    if data_type == "DateTime":
        return EPOCH
    return ""


def coerce(value: Any, data_type: str) -> Scalar:
    """`value` in the Python type that carries `data_type`. Raises Mismatch if it cannot be."""
    if data_type in ("DataSet", "Document"):
        return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    if data_type == "String":
        return value if isinstance(value, str) else str(value)
    if data_type == "Boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float) and value in (0, 1):
            return bool(value)
        raise Mismatch(f"{value!r} is not a Boolean")
    if data_type == "DateTime":
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as e:
                raise Mismatch(f"{value!r} is not a DateTime") from e
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return datetime.fromtimestamp(float(value), UTC)
        raise Mismatch(f"{value!r} is not a DateTime")
    if isinstance(value, bool) or not isinstance(value, int | float):
        if isinstance(value, bool):
            return int(value) if data_type.startswith("Int") else float(value)
        raise Mismatch(f"{value!r} is not a number")
    if data_type.startswith("Float"):
        return float(value)
    if data_type.startswith("Int"):
        if not math.isfinite(value):
            raise Mismatch(f"{value!r} is not an integer")
        number = int(round(value))
        low, high = _INT_RANGES[data_type]
        if not low <= number <= high:
            raise Mismatch(f"{value!r} is outside {data_type}")
        return number
    raise Mismatch(f"unknown data type {data_type!r}")

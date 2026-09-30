"""The recent trajectory of a runtime session: trends, the alarm log and fault propagation.

The runtime publishes one frame per step. The History keeps the last `capacity` of them as
columns (one float array per point value and per numeric state signal), so a trend, an alarm
list or a propagation timeline can be read back after the frames have gone by. Everything
here is derived from recorded frames: nothing decides what a fault does.

- **Alarms.** A point whose binding is a `fault_alarm` raises an alarm when it turns true and
  clears it when it turns false. Each alarm names the most recent trajectory event before it
  (usually the fault that was injected) as context.
- **Propagation.** From a moment (usually a fault), each asset's state signals are compared
  with their values at that moment; an asset appears at the first step one of them moves by
  more than a threshold for its unit. The result is the order in which the consequences
  reached each asset.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

QUALITY = {"good": 0, "uncertain": 1, "bad": 2}

Series = tuple[str, str, str]
"""(`point`, path, "") or (`state`, asset, signal)."""


@dataclass
class Alarm:
    id: int
    point: str
    asset: str | None
    raised_t: float
    cleared_t: float | None = None
    context: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "point": self.point,
            "asset": self.asset,
            "raised_t": self.raised_t,
            "cleared_t": self.cleared_t,
            "active": self.cleared_t is None,
            "context": self.context,
        }


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float) and math.isfinite(value):
        return float(value)
    return math.nan


# Change thresholds for propagation, by unit: an absolute floor and a fraction of the value.
THRESHOLDS: dict[str | None, tuple[float, float]] = {
    "K": (0.3, 0.0),
    "degC": (0.3, 0.0),
    "W": (500.0, 0.03),
    "kg/s": (0.05, 0.03),
    "Pa": (500.0, 0.03),
    "1": (0.02, 0.0),
    "pu": (0.01, 0.0),
    None: (1e-6, 0.03),
}


def threshold(unit: str | None, reference: float) -> float:
    floor, fraction = THRESHOLDS.get(unit, THRESHOLDS[None])
    return max(floor, fraction * abs(reference))


@dataclass
class History:
    capacity: int = 7200
    t: NDArray[np.float64] = field(init=False)
    columns: dict[Series, NDArray[np.float32]] = field(init=False, default_factory=dict)
    quality: dict[str, NDArray[np.uint8]] = field(init=False, default_factory=dict)
    count: int = field(init=False, default=0)
    alarms: list[Alarm] = field(init=False, default_factory=list)
    _raised: dict[str, Alarm] = field(init=False, default_factory=dict)
    _last_step: int | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.t = np.full(self.capacity, np.nan)

    # --- recording -----------------------------------------------------------------------

    def _slot(self) -> int:
        return self.count % self.capacity

    def _column(self, key: Series) -> NDArray[np.float32]:
        col = self.columns.get(key)
        if col is None:
            col = np.full(self.capacity, np.nan, dtype=np.float32)
            self.columns[key] = col
        return col

    def record(
        self,
        frame: Mapping[str, Any],
        alarm_points: Callable[[], Mapping[str, str | None]],
        context: Callable[[float], dict[str, Any] | None],
    ) -> None:
        """Add a frame. `alarm_points` gives each alarm point's asset; `context` the event
        that preceded a time. A frame for a step already recorded (a re-sent current frame)
        replaces nothing."""
        step = int(frame.get("step", -1))
        if self._last_step is not None and step == self._last_step:
            return
        if self._last_step is not None and step < self._last_step:
            self.clear()  # restored to an earlier snapshot: a new branch
        self._last_step = step
        slot = self._slot()
        t = float(frame["t"])
        self.t[slot] = t
        for col in self.columns.values():
            col[slot] = np.nan
        for path, p in frame.get("points", {}).items():
            self._column(("point", path, ""))[slot] = _number(p.get("value"))
            q = self.quality.get(path)
            if q is None:
                q = np.full(self.capacity, 2, dtype=np.uint8)
                self.quality[path] = q
            q[slot] = QUALITY.get(str(p.get("quality")), 2)
        for asset, signals in frame.get("state", {}).items():
            for signal, value in signals.items():
                if isinstance(value, int | float | bool):
                    self._column(("state", asset, signal))[slot] = _number(value)
        self.count += 1
        self._alarms(t, frame.get("points", {}), alarm_points(), context)

    def _alarms(
        self,
        t: float,
        points: Mapping[str, Any],
        alarm_points: Mapping[str, str | None],
        context: Callable[[float], dict[str, Any] | None],
    ) -> None:
        for path, asset in alarm_points.items():
            p = points.get(path)
            on = bool(p and p.get("quality") == "good" and p.get("value") is True)
            raised = self._raised.get(path)
            if on and raised is None:
                alarm = Alarm(len(self.alarms) + 1, path, asset, t, context=context(t))
                self.alarms.append(alarm)
                self._raised[path] = alarm
            elif not on and raised is not None:
                raised.cleared_t = t
                del self._raised[path]

    def clear(self) -> None:
        self.t[:] = np.nan
        self.columns.clear()
        self.quality.clear()
        self.count = 0
        for alarm in self._raised.values():
            alarm.cleared_t = alarm.cleared_t or alarm.raised_t
        self._raised.clear()

    # --- reading -------------------------------------------------------------------------

    def _order(self) -> NDArray[np.int64]:
        n = min(self.count, self.capacity)
        start = self.count - n
        return (np.arange(start, self.count) % self.capacity).astype(np.int64)

    def window(self, since: float | None = None, until: float | None = None) -> NDArray[np.int64]:
        idx = self._order()
        times = self.t[idx]
        keep = np.ones(len(idx), dtype=bool)
        if since is not None:
            keep &= times >= since
        if until is not None:
            keep &= times <= until
        return idx[keep]

    def series(
        self,
        keys: Iterable[Series],
        since: float | None = None,
        until: float | None = None,
        max_points: int = 2000,
    ) -> dict[str, Any]:
        """Times and values of each series, thinned to at most `max_points` samples."""
        idx = self.window(since, until)
        if len(idx) > max_points:
            idx = idx[np.linspace(0, len(idx) - 1, max_points).round().astype(np.int64)]
        out: dict[str, Any] = {"t": self.t[idx].tolist(), "series": []}
        for kind, a, b in keys:
            col = self.columns.get((kind, a, b))
            values = None
            if col is not None:
                values = [None if math.isnan(v) else v for v in col[idx].tolist()]
            entry: dict[str, Any] = {"kind": kind, "values": values}
            if kind == "point":
                entry["path"] = a
                q = self.quality.get(a)
                inv = {v: k for k, v in QUALITY.items()}
                entry["quality"] = [inv[int(x)] for x in q[idx]] if q is not None else None
            else:
                entry["asset"], entry["signal"] = a, b
            out["series"].append(entry)
        return out

    def propagation(
        self,
        since: float,
        units: Mapping[tuple[str, str], str | None],
        until: float | None = None,
    ) -> list[dict[str, Any]]:
        """Each asset whose state moved after `since`, at the first step it moved, in order.
        Each change gives the value at `since`, at that first step, and at the window's end."""
        idx = self.window(since, until)
        if len(idx) < 2:
            return []
        times = self.t[idx]
        first: dict[str, dict[str, Any]] = {}
        for (kind, asset, signal), col in self.columns.items():
            if kind != "state":
                continue
            values = col[idx].astype(np.float64)
            base = values[0]
            if math.isnan(base):
                continue
            unit = units.get((asset, signal))
            limit = threshold(unit, base)
            moved = np.nonzero(np.abs(values - base) > limit)[0]
            if len(moved) == 0:
                continue
            k = int(moved[0])
            t = float(times[k])
            change = {
                "signal": signal,
                "unit": unit,
                "t": t,
                "before": float(base),
                "at": float(values[k]),
                "after": float(values[-1]),
            }
            entry = first.setdefault(asset, {"asset": asset, "t": t, "changes": []})
            entry["changes"].append(change)
            entry["t"] = min(entry["t"], t)
        for entry in first.values():
            entry["changes"].sort(key=lambda c: (c["t"], c["signal"]))
        return sorted(first.values(), key=lambda e: (e["t"], e["asset"]))

    @property
    def span(self) -> tuple[float, float] | None:
        idx = self._order()
        if len(idx) == 0:
            return None
        return float(self.t[idx[0]]), float(self.t[idx[-1]])

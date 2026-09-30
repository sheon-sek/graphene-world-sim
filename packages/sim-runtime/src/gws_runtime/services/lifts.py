"""Passenger lifts in the central core, Ground (level 1) to Roof (level 4).

The cause is traffic: passengers call a lift at a rate (an operating condition), each call
from one level to another. The calls are a Poisson process drawn from a stable hash of the lift
and the call's number, so a run is reproducible. A lift travels at `FLOOR_S` a level plus its
start and stop, opens its doors for `DOOR_S`, and serves calls in order.

A fire zone connected to a lift recalls it: it returns to level 1, opens its doors and takes
no calls until the recall ends. A lift without supply stops where it is with its doors shut.
An `out_of_service` fault holds it the same way until it is reset.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from gws_runtime.services.common import Env, Signals, param, unit_fraction
from gws_world_model.model import WorldModel

LIFT = "Lift"
LEVELS = 4
FLOOR_S = 2.5
START_STOP_S = 3.0
DOOR_S = 6.0
V_TRIP = 0.85


@dataclass(slots=True)
class _Lift:
    asset: str
    rated_w: float
    standby_w: float
    level: int = 1
    target: int = 1
    """Level it is moving to (equal to `level` when stationary)."""
    arrive_t: float = 0.0
    depart_level: int = 1
    depart_t: float = 0.0
    doors_until: float = 0.0
    calls: int = 0
    next_call_t: float = -1.0
    pending: tuple[int, int] | None = None
    """A call being served: (from level, to level)."""

    @property
    def moving(self) -> bool:
        return self.target != self.level


@dataclass
class Lifts:
    lifts: dict[str, _Lift]
    faults: dict[str, set[str]]

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> Lifts:
        lifts = {
            a: _Lift(
                asset=a,
                rated_w=param(doc, a, "rated_power", 30.0) * 1e3,
                standby_w=param(doc, a, "standby_power", 1.5) * 1e3,
            )
            for a in sorted(scope)
            if doc.assets[a].type == LIFT
        }
        return cls(lifts, {})

    @property
    def assets(self) -> set[str]:
        return set(self.lifts)

    def fault(self, asset: str, mode: str, values: Mapping[str, float]) -> None:
        self.faults.setdefault(asset, set()).add(mode)

    def clear(self, asset: str, mode: str) -> None:
        self.faults.get(asset, set()).discard(mode)

    def _next_call(self, lift: _Lift, t: float, per_hour: float) -> None:
        if per_hour <= 0:
            lift.next_call_t = math.inf
            return
        u = unit_fraction(f"{lift.asset}#{lift.calls}")
        lift.next_call_t = t + -math.log(1 - u) * 3600 / per_hour

    def _call(self, lift: _Lift) -> tuple[int, int]:
        n = lift.calls
        start = 1 + int(unit_fraction(f"{lift.asset}#{n}:from") * LEVELS)
        to = 1 + int(unit_fraction(f"{lift.asset}#{n}:to") * (LEVELS - 1))
        return start, to if to < start else to + 1

    def _go(self, lift: _Lift, t: float, level: int) -> None:
        lift.depart_level, lift.depart_t = lift.level, t
        lift.target = level
        lift.arrive_t = t + START_STOP_S + FLOOR_S * abs(level - lift.level)

    def step(self, env: Env, recalled: Mapping[str, bool], per_hour: float) -> None:
        t = env.t
        for lift in self.lifts.values():
            if lift.next_call_t < 0:
                self._next_call(lift, t, per_hour)
            held = bool(self.faults.get(lift.asset)) or env.supply(lift.asset) < V_TRIP
            if held:
                if lift.moving:  # stops at the level it was passing
                    lift.level = self._passing(lift, t)
                    lift.target = lift.level
                lift.doors_until = 0.0
                continue
            if lift.moving and t >= lift.arrive_t:
                lift.level = lift.target
                lift.doors_until = t + DOOR_S
            if recalled.get(lift.asset):
                lift.pending = None
                if lift.level != 1 and not lift.moving:
                    self._go(lift, t, 1)
                elif lift.level == 1 and not lift.moving:
                    lift.doors_until = math.inf
                continue
            if lift.doors_until == math.inf:
                lift.doors_until = t  # the recall ended: the doors close
            while t >= lift.next_call_t and lift.pending is None:
                lift.pending = self._call(lift)
                lift.calls += 1
                self._next_call(lift, t, per_hour)
            if lift.pending is not None and not lift.moving and t >= lift.doors_until:
                start, to = lift.pending
                if lift.level != start:
                    self._go(lift, t, start)
                else:
                    self._go(lift, t, to)
                    lift.pending = None

    def _passing(self, lift: _Lift, t: float) -> int:
        travelled = max(t - lift.depart_t - START_STOP_S / 2, 0.0) / FLOOR_S
        step = 1 if lift.target > lift.depart_level else -1
        moved = min(int(travelled), abs(lift.target - lift.depart_level))
        return lift.depart_level + step * moved

    def demand_w(self, asset: str) -> float:
        lift = self.lifts[asset]
        return lift.rated_w if lift.moving else lift.standby_w

    def signals(self, env: Env) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for a, lift in self.lifts.items():
            if lift.moving:
                direction = "Up" if lift.target > lift.level else "Down"
                level = self._passing(lift, env.t)
            else:
                direction, level = "", lift.level
            out[a] = {
                "level": float(level),
                "direction": direction,
                "doorState": float(env.t < lift.doors_until and not lift.moving),
                "movingUntil": lift.arrive_t,
                "in_service": not self.faults.get(a),
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            a: {
                "level": x.level,
                "target": x.target,
                "arrive_t": x.arrive_t,
                "depart_level": x.depart_level,
                "depart_t": x.depart_t,
                "doors_until": None if math.isinf(x.doors_until) else x.doors_until,
                "calls": x.calls,
                "next_call_t": None if math.isinf(x.next_call_t) else x.next_call_t,
                "pending": list(x.pending) if x.pending is not None else None,
                "faults": sorted(self.faults.get(a, set())),
            }
            for a, x in self.lifts.items()
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        for a, x in s.items():
            if a not in self.lifts:
                continue
            lift = self.lifts[a]
            lift.level, lift.target = int(x["level"]), int(x["target"])
            lift.arrive_t, lift.depart_level = float(x["arrive_t"]), int(x["depart_level"])
            lift.depart_t = float(x["depart_t"])
            lift.doors_until = math.inf if x["doors_until"] is None else float(x["doors_until"])
            lift.calls = int(x["calls"])
            lift.next_call_t = math.inf if x["next_call_t"] is None else float(x["next_call_t"])
            pending = x["pending"]
            lift.pending = (int(pending[0]), int(pending[1])) if pending is not None else None
            self.faults[a] = set(x["faults"])

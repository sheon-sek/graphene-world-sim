"""Sensors that read the site's rooms and weather rather than a piece of equipment.

- **Room sensors** (environment monitoring, temperature and humidity) read the air of the
  room they are in, each with its own small offset: a sensor near a supply grille reads a
  little cooler than one by a hot aisle. The offset is a stable hash of the sensor, so it is
  the same every run.
- **Leak detection cables** lie on a room's floor. Water on the floor comes from a leak: an
  operating condition (`leaks`: a flow at a point in a room) or a leaking tank. It spreads
  until it reaches a cable once `DETECT_KG` has spilled, and the cable reports the distance
  along it to the leak. A room with several cables covers successive lengths from their
  controller. The floor drains at `DRAIN_KG_S` once the leak stops. A cable fault reports a
  cable break (status 1); a room below 2 degC reports freezing (status 2).
- **The weather station** reads the operating conditions.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.conditions import P_ATM, Conditions
from gws_runtime.services.common import Env, Signals, number, param, unit_fraction
from gws_world_model.model import ROOM_PREFIX, WorldModel

ROOM_SENSORS = frozenset({"Environment Monitoring", "Temperature and Humidity"})
LEAK = "Water Leak Cable Sensor"
WEATHER = "Weather Station"
T_SPREAD_K = 1.5
PHI_SPREAD = 0.04
DETECT_KG = 1.0
DRAIN_KG_S = 0.005
FREEZING_K = 275.15


@dataclass(frozen=True, slots=True)
class Leak:
    """Water escaping onto a room's floor."""

    flow_kg_s: float
    x: float | None = None
    y: float | None = None


@dataclass
class RoomSensors:
    room: dict[str, str]
    offset: dict[str, tuple[float, float]]

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> RoomSensors:
        room: dict[str, str] = {}
        offset: dict[str, tuple[float, float]] = {}
        for a in sorted(scope):
            asset = doc.assets[a]
            if asset.type in ROOM_SENSORS and asset.location.room:
                room[a] = asset.location.room
                offset[a] = (
                    T_SPREAD_K * (unit_fraction(f"{a}:T") - 0.5),
                    PHI_SPREAD * (unit_fraction(f"{a}:phi") - 0.5),
                )
        return cls(room, offset)

    def signals(self, env: Env) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for a, r in self.room.items():
            air = env.state.get(f"{ROOM_PREFIX}{r}", {})
            t, phi = number(air, "TAir"), number(air, "phi")
            s: Signals = {}
            if t is not None:
                s["T"] = t + self.offset[a][0]
            if phi is not None:
                s["phi"] = min(max(phi + self.offset[a][1], 0.0), 1.0)
            out[a] = s
        return out


@dataclass
class LeakDetection:
    cables: dict[str, list[str]]
    """Room -> its cables in order."""
    where: dict[str, tuple[float, float] | None]
    length: dict[str, float]
    spilled: dict[str, dict[str, float]] = field(default_factory=dict)
    """Room -> leak source -> water on the floor, kg."""
    point: dict[str, dict[str, tuple[float, float] | None]] = field(default_factory=dict)
    faults: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> LeakDetection:
        cables: dict[str, list[str]] = defaultdict(list)
        where: dict[str, tuple[float, float] | None] = {}
        length: dict[str, float] = {}
        for a in sorted(scope):
            asset = doc.assets[a]
            if asset.type != LEAK or not asset.location.room:
                continue
            cables[asset.location.room].append(a)
            loc = asset.location
            where[a] = (loc.x, loc.y) if loc.x is not None and loc.y is not None else None
            length[a] = param(doc, a, "cable_length", 20.0)
        return cls(dict(cables), where, length)

    @property
    def assets(self) -> set[str]:
        return {a for cs in self.cables.values() for a in cs}

    def step(self, env: Env, sources: Mapping[str, Mapping[str, Leak]]) -> None:
        """`sources`: room -> source name -> leak."""
        for room in self.cables:
            floor = self.spilled.setdefault(room, {})
            points = self.point.setdefault(room, {})
            now = sources.get(room, {})
            for name, leak in now.items():
                floor[name] = floor.get(name, 0.0) + max(leak.flow_kg_s, 0.0) * env.dt
                if leak.x is not None and leak.y is not None:
                    points[name] = (leak.x, leak.y)
                else:
                    points.setdefault(name, None)
            for name in [n for n in floor if n not in now]:
                floor[name] = max(floor[name] - DRAIN_KG_S * env.dt, 0.0)
                if floor[name] == 0.0:
                    del floor[name]
                    points.pop(name, None)

    def _positions(self, room: str) -> dict[str, float]:
        """Cable -> distance along it to the nearest detected leak."""
        cables = self.cables[room]
        out: dict[str, float] = {}
        for name, kg in sorted(self.spilled.get(room, {}).items()):
            if kg < DETECT_KG:
                continue
            origin = self.where[cables[0]]
            leak = self.point.get(room, {}).get(name)
            if origin is None or leak is None:
                d = 0.5 * self.length[cables[0]]
            else:
                d = math.hypot(leak[0] - origin[0], leak[1] - origin[1])
            covered = 0.0
            for i, c in enumerate(cables):
                span = self.length[c]
                if d < covered + span or i == len(cables) - 1:
                    pos = min(max(d - covered, 0.1), span)
                    out[c] = min(out.get(c, pos), pos)
                    break
                covered += span
        return out

    def signals(self, env: Env) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for room, cables in self.cables.items():
            air = number(env.state.get(f"{ROOM_PREFIX}{room}", {}), "TAir")
            positions = self._positions(room)
            for c in cables:
                broken = "cable_fault" in self.faults.get(c, set())
                status = 1.0 if broken else 2.0 if air is not None and air < FREEZING_K else 0.0
                out[c] = {
                    "leak_position": 0.0 if broken else positions.get(c, 0.0),
                    "status": status,
                    "cable_length": self.length[c],
                    "leak": c in positions and not broken,
                }
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "spilled": {r: dict(f) for r, f in self.spilled.items()},
            "point": {
                r: {n: list(p) if p else None for n, p in f.items()} for r, f in self.point.items()
            },
            "faults": {a: sorted(f) for a, f in self.faults.items()},
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        self.spilled = {r: {n: float(v) for n, v in f.items()} for r, f in s["spilled"].items()}
        self.point = {
            r: {n: (float(p[0]), float(p[1])) if p else None for n, p in f.items()}
            for r, f in s["point"].items()
        }
        self.faults = {a: set(f) for a, f in s["faults"].items()}


def dew_point_k(t_c: float, rh_pct: float) -> float:
    """Magnus formula, over water."""
    a, b = 17.62, 243.12
    g = math.log(max(rh_pct, 1e-3) / 100) + a * t_c / (b + t_c)
    return b * g / (a - g) + 273.15


def weather(c: Conditions) -> Signals:
    return {
        "TDryBul": c.dry_bulb_c + 273.15,
        "TWetBul": c.wet_bulb_c + 273.15,
        "relHum": c.relative_humidity / 100,
        "TDewPoi": dew_point_k(c.dry_bulb_c, c.relative_humidity),
        "pAtm": P_ATM,
        "precipitation": c.precipitation_mm / 1e3,
        "winDir": math.radians(c.wind_direction_deg),
        "winSpe": c.wind_speed_m_s,
        "status": "OK",
    }

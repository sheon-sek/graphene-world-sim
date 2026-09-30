"""Operating conditions (ADR-0002): the inputs from outside the facility that an engineer
changes live — weather, each hall's IT load, whether the utility supply and the water main
are there — and the events that happen to the building: a fire in a room, water leaking onto a
floor, the lift traffic.

They start from the World Model's `conditions` and change only by an explicit command, which
the lifecycle's event log records. The models react: the wet bulb is a cooling tower input,
so a hotter, more humid day raises the condenser water temperature the towers can reach; the
IT load is electrical demand, and the power actually drawn is heat in the hall.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_world_model.model import Conditions as WorldConditions

P_ATM = 101325.0


def saturation_pressure(t_k: float) -> float:
    """Water vapour saturation pressure over liquid water, Pa (as in the Buildings library)."""
    return 611.657 * math.exp(17.2799 - 4102.99 / (t_k - 35.719))


def vapour_fraction(t_c: float, rh_pct: float, p: float = P_ATM) -> float:
    """Water vapour mass fraction of moist air (kg water per kg moist air)."""
    pw = rh_pct / 100 * saturation_pressure(t_c + 273.15)
    x = 0.621964 * pw / (p - pw)
    return x / (1 + x)


@dataclass
class Conditions:
    dry_bulb_c: float = 30.0
    wet_bulb_c: float = 25.0
    relative_humidity: float = 70.0
    utility_available: bool = True
    it_load_kw: dict[str, float] = field(default_factory=dict)
    """Room -> design IT load, kW."""
    it_fraction: dict[str, float] = field(default_factory=dict)
    """Room -> operating fraction of design."""
    liquid_fraction: dict[str, float] = field(default_factory=dict)
    """Room -> share of its IT heat removed by liquid cooling (CDUs) rather than the air."""
    wind_speed_m_s: float = 3.0
    wind_direction_deg: float = 180.0
    precipitation_mm: float = 0.0
    water_mains_available: bool = True
    lift_trips_per_hour: float = 12.0
    """Passenger calls per hour on each lift."""
    fires: dict[str, dict[str, float]] = field(default_factory=dict)
    """Room -> {`smoke_pct_m`: obscuration, `temperature_c`: hot-layer temperature}."""
    leaks: dict[str, dict[str, float]] = field(default_factory=dict)
    """Room -> {`flow_kg_s`, and optionally `x`, `y` in plan metres}: water onto the floor."""

    @classmethod
    def from_world(cls, c: WorldConditions) -> Conditions:
        return cls(
            dry_bulb_c=c.weather.dry_bulb_c,
            wet_bulb_c=c.weather.wet_bulb_c,
            relative_humidity=c.weather.relative_humidity,
            utility_available=c.utility_available,
            it_load_kw={k: v.design_kw for k, v in c.it_load.items()},
            it_fraction={k: v.fraction for k, v in c.it_load.items()},
            liquid_fraction={k: v.liquid_fraction for k, v in c.it_load.items()},
        )

    def environment(self) -> dict[str, float]:
        """Values of the `env_*` model inputs, SI."""
        return {
            "TWetBulb": self.wet_bulb_c + 273.15,
            "TDryBulb": self.dry_bulb_c + 273.15,
            "XOut": vapour_fraction(self.dry_bulb_c, self.relative_humidity),
        }

    def it_demand_w(self, room: str) -> float:
        return self.it_load_kw.get(room, 0.0) * 1e3 * self.it_fraction.get(room, 0.0)

    def set(self, changes: Mapping[str, Any]) -> None:
        """Apply a change: any of `dry_bulb_c`, `wet_bulb_c`, `relative_humidity`,
        `wind_speed_m_s`, `wind_direction_deg`, `precipitation_mm`, `utility_available`,
        `water_mains_available`, `lift_trips_per_hour`, `it_fraction` as {room: fraction},
        or `fires` / `leaks` as {room: {...}} (the whole set: a room left out has none)."""
        for key, value in changes.items():
            if key == "it_fraction":
                for room, fraction in dict(value).items():
                    if room not in self.it_load_kw:
                        raise KeyError(f"no IT load is defined for room {room!r}")
                    if not 0 <= float(fraction) <= 1.5:
                        raise ValueError(f"IT load fraction {fraction} is outside 0–1.5")
                    self.it_fraction[room] = float(fraction)
            elif key in ("utility_available", "water_mains_available"):
                setattr(self, key, bool(value))
            elif key in (
                "dry_bulb_c",
                "wet_bulb_c",
                "relative_humidity",
                "wind_speed_m_s",
                "wind_direction_deg",
                "precipitation_mm",
                "lift_trips_per_hour",
            ):
                setattr(self, key, float(value))
            elif key == "fires":
                self.fires = _events(value, {"smoke_pct_m", "temperature_c"}, key)
            elif key == "leaks":
                self.leaks = _events(value, {"flow_kg_s", "x", "y"}, key)
            else:
                raise KeyError(f"unknown operating condition {key!r}")
        if self.wet_bulb_c > self.dry_bulb_c:
            raise ValueError("the wet bulb cannot exceed the dry bulb")

    def snapshot(self) -> dict[str, Any]:
        return {
            "dry_bulb_c": self.dry_bulb_c,
            "wet_bulb_c": self.wet_bulb_c,
            "relative_humidity": self.relative_humidity,
            "utility_available": self.utility_available,
            "it_load_kw": dict(self.it_load_kw),
            "it_fraction": dict(self.it_fraction),
            "liquid_fraction": dict(self.liquid_fraction),
            "wind_speed_m_s": self.wind_speed_m_s,
            "wind_direction_deg": self.wind_direction_deg,
            "precipitation_mm": self.precipitation_mm,
            "water_mains_available": self.water_mains_available,
            "lift_trips_per_hour": self.lift_trips_per_hour,
            "fires": {r: dict(f) for r, f in self.fires.items()},
            "leaks": {r: dict(f) for r, f in self.leaks.items()},
        }

    @classmethod
    def from_snapshot(cls, d: Mapping[str, Any]) -> Conditions:
        return cls(
            dry_bulb_c=float(d["dry_bulb_c"]),
            wet_bulb_c=float(d["wet_bulb_c"]),
            relative_humidity=float(d["relative_humidity"]),
            utility_available=bool(d["utility_available"]),
            it_load_kw={k: float(v) for k, v in d["it_load_kw"].items()},
            it_fraction={k: float(v) for k, v in d["it_fraction"].items()},
            liquid_fraction={k: float(v) for k, v in d.get("liquid_fraction", {}).items()},
            wind_speed_m_s=float(d.get("wind_speed_m_s", 3.0)),
            wind_direction_deg=float(d.get("wind_direction_deg", 180.0)),
            precipitation_mm=float(d.get("precipitation_mm", 0.0)),
            water_mains_available=bool(d.get("water_mains_available", True)),
            lift_trips_per_hour=float(d.get("lift_trips_per_hour", 12.0)),
            fires={r: dict(f) for r, f in d.get("fires", {}).items()},
            leaks={r: dict(f) for r, f in d.get("leaks", {}).items()},
        )


def _events(value: Any, fields: set[str], key: str) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for room, spec in dict(value or {}).items():
        unknown = set(spec) - fields
        if unknown:
            raise KeyError(f"{key} of {room!r} has no fields {sorted(unknown)}")
        out[str(room)] = {k: float(v) for k, v in spec.items()}
    return out

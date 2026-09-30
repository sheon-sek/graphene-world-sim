"""Diesel fuel: the bulk tanks, their transfer pumps, and the gensets' day tanks.

Each genset burns fuel from its own day tank in proportion to its load (a tenth of full-load
consumption at idle). A bulk tank's fuel pump tops up the day tanks of the gensets it is
connected to: it starts when one falls below `DAY_START` and stops when all are above
`DAY_STOP`. A genset whose day tank runs dry stops (the electrical model locks it out on
fuel). The bulk tank's contamination leaves it unable to supply, a pump trip stops the pump
until reset, and a stuck inlet valve raises the inlet's time-out alarm.

Fuel volumes are published in m3 and flows as water-equivalent kg/s (a litre per second is
1 kg/s), because the unit table converts volume flows through the density of water.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.services.common import Env, Signals, number, param
from gws_world_model.model import Domain, WorldModel

DIESEL = "Diesel"
DIESEL_KG_M3 = 840.0
DAY_TANK_M3 = 1.0
"""Day tank of each genset (assumed; the site data has none)."""
DAY_START, DAY_STOP = 0.8, 0.95
IDLE_FUEL = 0.1
LOW_BULK = 0.2
V_TRIP = 0.85


@dataclass(slots=True)
class _Bulk:
    asset: str
    capacity_m3: float
    level: float
    pump_kg_s: float
    pump_w: float
    gensets: list[str]
    pumping: bool = False
    flow_kg_s: float = 0.0
    total_m3: float = 0.0
    manual: bool | None = None


@dataclass
class FuelSystem:
    bulks: dict[str, _Bulk]
    day: dict[str, float]
    """Genset -> its day tank level, fraction of full."""
    burn: dict[str, tuple[float, float]]
    """Genset -> (full-load fuel flow kg/s, prime power W)."""
    faults: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> FuelSystem:
        fed: dict[str, list[str]] = defaultdict(list)
        for c in sorted(doc.connections.values(), key=lambda c: c.id):
            if c.domain is Domain.FUEL and c.source.node in scope and c.target.node in scope:
                fed[c.source.node].append(c.target.node)
        bulks = {
            a: _Bulk(
                asset=a,
                capacity_m3=param(doc, a, "volume", 50000.0) / 1e3,
                level=param(doc, a, "level_start", 0.9),
                pump_kg_s=param(doc, a, "pump_flow_nominal", 0.5),
                pump_w=param(doc, a, "pump_power", 3.0) * 1e3,
                gensets=sorted(fed.get(a, [])),
            )
            for a in sorted(scope)
            if doc.assets[a].type == DIESEL
        }
        gensets = sorted({g for b in bulks.values() for g in b.gensets})
        burn = {
            g: (param(doc, g, "fuel_flow_full_load", 0.19), param(doc, g, "p_prime", 3000.0) * 1e3)
            for g in gensets
        }
        return cls(bulks, {g: DAY_STOP for g in gensets}, burn)

    @property
    def assets(self) -> set[str]:
        return set(self.bulks)

    def fault(self, asset: str, mode: str, values: Mapping[str, float]) -> None:
        self.faults.setdefault(asset, {})[mode] = dict(values)

    def clear(self, asset: str, mode: str) -> None:
        self.faults.get(asset, {}).pop(mode, None)

    def command(self, asset: str, signal: str, value: float | bool) -> bool:
        bulk = self.bulks.get(asset)
        if bulk is None:
            return False
        if signal in ("start", "stop"):
            bulk.manual = signal == "start"
        elif signal == "auto":
            bulk.manual = None
        elif signal == "Run_Stop - Fuel Pump A":
            bulk.manual = bool(value)
        else:
            return False
        return True

    def _has(self, asset: str, mode: str) -> bool:
        return mode in self.faults.get(asset, {})

    def step(self, env: Env) -> None:
        dt = env.dt
        for g, (full, prime) in self.burn.items():
            s = env.state.get(g, {})
            if s.get("running") is True:
                load = max(number(s, "P") or 0.0, 0.0) / prime if prime > 0 else 0.0
                kg = full * (IDLE_FUEL + (1 - IDLE_FUEL) * min(load, 1.2)) * dt
                self.day[g] = max(self.day[g] - kg / DIESEL_KG_M3 / DAY_TANK_M3, 0.0)
        for bulk in self.bulks.values():
            a = bulk.asset
            want = any(self.day[g] < DAY_START for g in bulk.gensets)
            full = all(self.day[g] >= DAY_STOP for g in bulk.gensets)
            if bulk.manual is None:
                bulk.pumping = want or (bulk.pumping and not full)
            else:
                bulk.pumping = bulk.manual
            if self._has(a, "pump_trip") or env.supply(a) < V_TRIP:
                bulk.pumping = False
            supply = not self._has(a, "contamination") and bulk.level > 0.0
            needing = [g for g in bulk.gensets if self.day[g] < 1.0]
            bulk.flow_kg_s = bulk.pump_kg_s if bulk.pumping and supply and needing else 0.0
            m3 = bulk.flow_kg_s * dt / DIESEL_KG_M3
            m3 = min(m3, bulk.level * bulk.capacity_m3)
            bulk.level -= m3 / bulk.capacity_m3
            bulk.total_m3 += m3
            for g in needing:
                self.day[g] = min(self.day[g] + m3 / len(needing) / DAY_TANK_M3, 1.0)

    def fuel_ok(self, genset: str) -> bool:
        return self.day.get(genset, 1.0) > 0.0

    def demand_w(self, asset: str) -> float:
        bulk = self.bulks[asset]
        return bulk.pump_w if bulk.pumping else 0.0

    def signals(self, env: Env) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for a, bulk in self.bulks.items():
            powered = env.supply(a) >= V_TRIP
            tripped = self._has(a, "pump_trip")
            low = bulk.level < LOW_BULK or self._has(a, "contamination")
            timeout = self._has(a, "inlet_valve_stuck")
            out[a] = {
                "level": bulk.level,
                "fuel_flow": bulk.flow_kg_s / DIESEL_KG_M3 * 1e3,
                "fuel_total": bulk.total_m3,
                "pump_running": bulk.pumping,
                "pump_tripped": tripped,
                "panel_on": powered,
                "panel_fault": not powered,
                "inlet_open": True,
                "inlet_timeout": timeout,
                "low_level": low,
                "alarm": tripped or low or timeout or not powered,
            }
        for g, level in self.day.items():
            out[g] = {"day_tank": level}
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "bulks": {
                a: {
                    "level": b.level,
                    "pumping": b.pumping,
                    "total_m3": b.total_m3,
                    "manual": b.manual,
                }
                for a, b in self.bulks.items()
            },
            "day": dict(self.day),
            "faults": {a: {m: dict(v) for m, v in f.items()} for a, f in self.faults.items()},
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        for a, x in s["bulks"].items():
            if a not in self.bulks:
                continue
            b = self.bulks[a]
            b.level, b.pumping = float(x["level"]), bool(x["pumping"])
            b.total_m3, b.manual = float(x["total_m3"]), x["manual"]
        self.day |= {k: float(v) for k, v in s["day"].items() if k in self.day}
        self.faults = {a: {m: dict(v) for m, v in f.items()} for a, f in s["faults"].items()}

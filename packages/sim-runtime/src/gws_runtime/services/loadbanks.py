"""Loads with no model of their own: the demo rack's load banks and the control room's small
power, and the branch circuits of the E820 in front of a load bank.

Each load wanders smoothly between a share of its full power (`min_fraction`) and full power
over its `cycle`, seeded by the asset so the same run always draws the same: a demo rack load
bank steps through a quick demonstration cycle, small power swings slowly.
The electrical network carries the demand to the meters in front of it, which measure it like
any other feeder. A tripped load bank draws nothing until reset.

A load bank behind a branch circuit monitor is split over the monitor's circuits (its point
members `<circuit>_P`). Each circuit has its own share and cycle, and the bank's demand is
their sum. The monitor's circuit readings come from here: a circuit is single-phase, so its
current is its apparent power over the phase voltage the network gives the monitor.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.services.common import Env, Signals, number, param, smooth, unit_fraction
from gws_world_model.model import Domain, WorldModel

LOAD_TYPES = frozenset({"Demo Load", "Small Power"})
V_TRIP = 0.85
CIRCUIT = re.compile(r".+_I\d+_(P|Q|S|PF|Current)")
CIRCUIT_UNITS = {"P": "W", "Q": "var", "S": "VA", "PF": "1", "Current": "A"}


@dataclass(slots=True)
class _Bank:
    rating_w: float
    power_factor: float
    cycle_s: float
    min_fraction: float
    meter: str | None
    circuits: dict[str, float]
    """Circuit -> its share of the rating (empty: the bank is one load)."""


def _circuits(doc: WorldModel, meter: str) -> list[str]:
    members = doc.component_types[doc.assets[meter].type].point_template
    return sorted(m[:-2] for m in members if m.endswith("_P") and f"{m[:-2]}_Current" in members)


@dataclass
class LoadBanks:
    banks: dict[str, _Bank]
    faults: dict[str, set[str]] = field(default_factory=dict)
    demand: dict[str, float] = field(default_factory=dict)
    """Load bank -> the demand it drew at the last step, W."""

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> LoadBanks:
        feeder: dict[str, str] = {}
        for c in doc.connections.values():
            if c.domain is Domain.POWER and c.source.node in doc.assets:
                feeder[c.target.node] = c.source.node
        banks: dict[str, _Bank] = {}
        for a in sorted(scope):
            if doc.assets[a].type not in LOAD_TYPES:
                continue
            meter = feeder.get(a)
            names = _circuits(doc, meter) if meter is not None else []
            weights = {n: 0.25 + unit_fraction(f"{meter}/{n}:share") for n in names}
            total = sum(weights.values())
            banks[a] = _Bank(
                rating_w=param(doc, a, "design_power", 5.0) * 1e3,
                power_factor=param(doc, a, "power_factor", 0.9),
                cycle_s=param(doc, a, "cycle", 120.0),
                min_fraction=param(doc, a, "min_fraction", 0.5),
                meter=meter if names else None,
                circuits={n: w / total for n, w in weights.items()},
            )
        rack = cls(banks)
        rack.demand = {a: sum(rack._circuit_w(b, a, 0.0).values()) for a, b in banks.items()}
        return rack

    @property
    def assets(self) -> set[str]:
        return set(self.banks)

    def fault(self, asset: str, mode: str, values: Mapping[str, float]) -> None:
        self.faults.setdefault(asset, set()).add(mode)

    def clear(self, asset: str, mode: str) -> None:
        self.faults.get(asset, set()).discard(mode)

    def command(self, asset: str, signal: str, value: float | bool) -> bool:
        if signal == "reset" and asset in self.banks:
            self.faults.pop(asset, None)
            return True
        return False

    @staticmethod
    def _level(bank: _Bank, key: str, t: float) -> float:
        low = bank.min_fraction
        return low + (1 - low) * smooth(f"{key}:duty", t, bank.cycle_s)

    def _circuit_w(self, bank: _Bank, name: str, t: float) -> dict[str, float]:
        if not bank.circuits:
            return {"": bank.rating_w * self._level(bank, name, t)}
        return {
            c: bank.rating_w * share * self._level(bank, f"{bank.meter}/{c}", t)
            for c, share in bank.circuits.items()
        }

    def _running(self, asset: str, env: Env) -> bool:
        return "trip" not in self.faults.get(asset, set()) and env.supply(asset) >= V_TRIP

    def step(self, env: Env) -> None:
        for asset, bank in self.banks.items():
            draw = sum(self._circuit_w(bank, asset, env.t).values())
            self.demand[asset] = draw if self._running(asset, env) else 0.0

    def demand_w(self, asset: str) -> float:
        return self.demand.get(asset, 0.0)

    def signals(self, env: Env) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for asset, bank in self.banks.items():
            running = self._running(asset, env)
            out[asset] = {"running": running, "tripped": "trip" in self.faults.get(asset, set())}
            if bank.meter is None:
                continue
            v = number(env.state.get(bank.meter, {}), "V_ln") or 0.0
            tan = math.tan(math.acos(bank.power_factor))
            circuits: Signals = {}
            for c, p in self._circuit_w(bank, asset, env.t).items():
                p = p if running else 0.0
                s = p / bank.power_factor
                circuits |= {
                    f"{c}_P": p,
                    f"{c}_Q": p * tan,
                    f"{c}_S": s,
                    f"{c}_PF": bank.power_factor if p > 0 else 0.0,
                    f"{c}_Current": s / v if v > 0 else 0.0,
                    f"{c}_RackID": c.split("_", 1)[0],
                }
            out.setdefault(bank.meter, {}).update(circuits)
        return out

    @staticmethod
    def unit(signal: str) -> str | None:
        """The unit of a branch circuit reading."""
        match = CIRCUIT.fullmatch(signal)
        return CIRCUIT_UNITS[match[1]] if match else None

    def snapshot(self) -> dict[str, Any]:
        return {"faults": {a: sorted(f) for a, f in self.faults.items()}}

    def restore(self, s: Mapping[str, Any]) -> None:
        self.faults = {a: set(f) for a, f in s.get("faults", {}).items()}

"""The control room's isolated power panel: what its transformer and monitors report.

The electrical network carries the panel's load like any feeder's. From it, and from the
panel's circuits and room, the panel reports:

- **Load** as a percentage of the isolation transformer's rating, and whether it is within it.
- **Transformer temperature**: the winding's rise over the room follows the square of the load
  with a first-order lag (`TAU_S`). Past `T_ALARM_K` the over-temperature contact closes. A
  ventilation failure multiplies the rise.
- **Insulation**: the insulation monitor reads the isolated system's resistance to earth. A
  healthy system reads `insulation_healthy`, lower in humid air; a circuit with an insulation
  fault brings it down to that circuit's share, and the first fault is what the monitor exists
  to catch.
- **Supervision**: the PE connection and the load CT (open or short), which only the panel's
  own fault modes change.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.services.common import Env, Signals, number, param
from gws_world_model.model import ROOM_PREFIX, Domain, WorldModel

IPS_PANEL = "IPS Panel"
TAU_S = 1200.0
RISE_K = 45.0
"""Winding rise over the room at full load."""
T_ALARM_K = 70.0 + 273.15
DEFAULT_ROOM_K = 295.15
FAULT_FRACTION = 0.05
"""Insulation left on a circuit with a first insulation fault."""


@dataclass(slots=True)
class _Panel:
    s_rated_va: float
    healthy_kohm: float
    room: str | None
    circuits: list[str]
    t_winding: float | None = None


@dataclass
class IpsPanels:
    panels: dict[str, _Panel]
    faults: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> IpsPanels:
        panels: dict[str, _Panel] = {}
        for a in sorted(scope):
            if doc.assets[a].type != IPS_PANEL:
                continue
            circuits = sorted(
                c.target.node
                for c in doc.connections.values()
                if c.domain is Domain.POWER and c.source.node == a
            )
            panels[a] = _Panel(
                s_rated_va=param(doc, a, "s_rated", 10.0) * 1e3,
                healthy_kohm=param(doc, a, "insulation_healthy", 800.0),
                room=doc.assets[a].location.room,
                circuits=circuits,
            )
        return cls(panels)

    @property
    def assets(self) -> set[str]:
        return set(self.panels)

    def fault(self, asset: str, mode: str, values: Mapping[str, float]) -> None:
        self.faults.setdefault(asset, {})[mode] = dict(values)

    def clear(self, asset: str, mode: str) -> None:
        self.faults.get(asset, {}).pop(mode, None)

    def command(self, asset: str, signal: str, value: float | bool) -> bool:
        return False

    def _room(self, panel: _Panel, env: Env) -> Mapping[str, float | bool | str]:
        return env.state.get(f"{ROOM_PREFIX}{panel.room}", {}) if panel.room else {}

    def _loading(self, asset: str, panel: _Panel, env: Env) -> float:
        s = number(env.state.get(asset, {}), "S") or 0.0
        return s / panel.s_rated_va if panel.s_rated_va > 0 else 0.0

    def step(self, env: Env) -> None:
        for asset, panel in self.panels.items():
            room = number(self._room(panel, env), "TAir") or DEFAULT_ROOM_K
            factor = self.faults.get(asset, {}).get("cooling_loss", {}).get("rise_factor", 1.0)
            target = room + RISE_K * factor * self._loading(asset, panel, env) ** 2
            if panel.t_winding is None:
                panel.t_winding = target
            panel.t_winding += (target - panel.t_winding) * (1 - math.exp(-env.dt / TAU_S))

    def signals(self, env: Env) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for asset, panel in self.panels.items():
            faults = self.faults.get(asset, {})
            loading = self._loading(asset, panel, env)
            phi = number(self._room(panel, env), "phi")
            healthy = panel.healthy_kohm * (1.25 - 0.5 * (phi if phi is not None else 0.5))
            faulted = sum(
                env.state.get(c, {}).get("insulation_fault") is True for c in panel.circuits
            )
            share = len(panel.circuits) or 1
            insulation = healthy * (FAULT_FRACTION * share if faulted else 1.0) / max(faulted, 1)
            energised = env.supply(asset) >= 0.5
            hot = (panel.t_winding or 0.0) > T_ALARM_K
            ct = "ct_open" not in faults and "ct_short" not in faults
            out[asset] = {
                "load_pct": round(100 * loading) if ct else 0,
                "load_ok": loading <= 1.0,
                "T_winding": panel.t_winding or DEFAULT_ROOM_K,
                "over_temperature": hot,
                "insulation_kohm": round(min(insulation, healthy)),
                "pe_connected": "pe_disconnected" not in faults,
                "ct_open": "ct_open" in faults,
                "ct_short": "ct_short" in faults,
                "device_ok": energised and not faults and not hot and not faulted,
            }
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "t_winding": {a: p.t_winding for a, p in self.panels.items()},
            "faults": {a: {m: dict(v) for m, v in f.items()} for a, f in self.faults.items()},
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        for a, t in s.get("t_winding", {}).items():
            if a in self.panels:
                self.panels[a].t_winding = None if t is None else float(t)
        self.faults = {
            a: {m: dict(v) for m, v in f.items()} for a, f in s.get("faults", {}).items()
        }

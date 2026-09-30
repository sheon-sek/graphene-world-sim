"""What the control network's devices report about themselves: load, memory, temperature,
latency, uptime, and each switch port's traffic, errors, PoE draw and speed.

`network.py` decides which devices are up and reachable; this module gives an up device the
readings of a running one. Nothing here is a constant: every reading follows the device's
state or a smooth, seeded variation of its traffic, so the same run always reads the same.

- **Traffic.** Each switch port carries a share of traffic that varies smoothly around its
  own level: an uplink to another node carries more than an access port. A port whose link is
  down carries nothing and reads speed 0.
- **CPU** rises with the traffic the device forwards; **memory** drifts slowly around its own
  level; the device's **temperature** is its room's air plus the heat of its load.
- **Ping time** grows with the hops to the gateway and the traffic on the way.
- **Uptime** counts from the device's last start, and a device that goes down and comes back
  starts again from zero.
- **Error counts** accumulate with traffic, faster on the few ports with a poor cable.
- **PoE power** is drawn on the access ports that feed a powered device.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.network import LINK_UP, ControlNetwork
from gws_runtime.services.common import number, unit_fraction
from gws_world_model.model import ROOM_PREFIX, WorldModel

UNITS = {
    "CPU": "1",
    "Memory": "1",
    "Temperature": "K",
    "Ping Time": "s",
    "Uptime": "s",
}
PORT_UNITS = {
    "Error Count": "1",
    "In Utilization": "1",
    "Out Utilization": "1",
    "PoE Power": "W",
    "Speed": "bit/s",
}
PERIOD_S = 30.0
"""How quickly traffic wanders: the spacing of the seeded variation's control points."""
MAX_UPTIME_S = 120 * 86400.0
LINK_BPS = 1e9
POOR_CABLE = 0.1
"""Share of ports with a poor cable, which count errors a hundred times faster."""
POE_SHARE = 0.4
DEFAULT_ROOM_K = 298.15


def smooth(key: str, t: float, period: float = PERIOD_S) -> float:
    """A seeded value in [0, 1) that varies smoothly with time."""
    b = math.floor(t / period)
    f = t / period - b
    a, c = unit_fraction(f"{key}:{b}"), unit_fraction(f"{key}:{b + 1}")
    return a + (c - a) * f * f * (3 - 2 * f)


@dataclass
class DeviceTelemetry:
    network: ControlNetwork
    room: dict[str, str | None]
    boot_t: dict[str, float] = field(default_factory=dict)
    """Node -> simulation time it last started (negative: before the run began)."""
    errors: dict[str, float] = field(default_factory=dict)
    was_up: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_world(cls, doc: WorldModel, network: ControlNetwork) -> DeviceTelemetry:
        assets = [*network.graph.nodes, *network.views]
        room = {a: doc.assets[a].location.room if a in doc.assets else None for a in assets}
        boot = {
            n: -unit_fraction(f"{n}:uptime") * MAX_UPTIME_S for n in sorted(network.graph.nodes)
        }
        return cls(network, room, boot)

    def _utilisation(self, node: str, port: int, t: float, uplink: bool) -> tuple[float, float]:
        key = f"{node}/{port}"
        level = (
            (0.10 + 0.20 * unit_fraction(f"{key}:level"))
            if uplink
            else (0.005 + 0.08 * unit_fraction(f"{key}:level"))
        )
        swing = 0.6 + 0.8 * smooth(f"{key}:in", t)
        ratio = 0.5 + unit_fraction(f"{key}:ratio")
        u_in = min(level * swing, 1.0)
        u_out = min(level * ratio * (0.6 + 0.8 * smooth(f"{key}:out", t)), 1.0)
        return u_in, u_out

    def signals(
        self, t: float, dt: float, state: Mapping[str, Mapping[str, float | bool | str]]
    ) -> dict[str, dict[str, float | bool | str]]:
        net = self.network
        out: dict[str, dict[str, float | bool | str]] = {}
        load: dict[str, float] = {}
        for node in sorted(net.graph.nodes):
            up = net.up(node)
            if up and not self.was_up.get(node, True):
                self.boot_t[node] = t  # restarted
            self.was_up[node] = up
        for asset in sorted([*net.graph.nodes, *net.views]):
            node = net.views.get(asset, asset)
            up = net.up(node)
            values: dict[str, float | bool | str] = {}
            count = net.port_counts.get(asset, net.port_counts.get(node))
            traffic = 0.05 * smooth(f"{node}:host", t)
            if count is not None:
                uplinks = len(list(net.graph.adj[node]))
                for n in range(1, count + 1):
                    name = f"Ports/Port {n:02d}"
                    link = net.signal(asset, f"{name}/Link Status") == LINK_UP
                    u_in, u_out = self._utilisation(node, n, t, n <= uplinks) if link else (0, 0)
                    key = f"{node}/{n}"
                    rate = 1e-4 * (100.0 if unit_fraction(f"{key}:cable") < POOR_CABLE else 1.0)
                    if asset == node or node not in net.port_counts:
                        self.errors[key] = self.errors.get(key, 0.0) + rate * (u_in + u_out) * dt
                    poe = 0.0
                    if link and n > uplinks and unit_fraction(f"{key}:poe") < POE_SHARE:
                        poe = (
                            3.0
                            + 10.0 * unit_fraction(f"{key}:poe_w")
                            + 0.3 * smooth(f"{key}:poe", t)
                        )
                    values |= {
                        f"{name}/Description": f"Port {n:02d}",
                        f"{name}/In Utilization": u_in,
                        f"{name}/Out Utilization": u_out,
                        f"{name}/Error Count": math.floor(self.errors.get(key, 0.0)),
                        f"{name}/PoE Power": poe,
                        f"{name}/Speed": LINK_BPS if link else 0.0,
                    }
                    traffic += (u_in + u_out) / (2 * count)
            load[node] = traffic
            cpu = min(0.04 + 0.08 * unit_fraction(f"{node}:cpu") + 0.6 * traffic, 1.0)
            cpu += 0.02 * smooth(f"{node}:cpu", t)
            room = self.room.get(asset)
            air = number(state.get(f"{ROOM_PREFIX}{room}", {}), "TAir") if room else None
            values |= {
                "CPU": cpu,
                "Memory": 0.3
                + 0.3 * unit_fraction(f"{node}:mem")
                + 0.03 * smooth(f"{node}:mem", t, 600.0),
                "Temperature": (air or DEFAULT_ROOM_K) + 12.0 + 15.0 * cpu,
                "Uptime": max(t - self.boot_t.get(node, 0.0), 0.0),
            }
            hops = net.path(asset)
            path_load = max((load.get(h, 0.0) for h in hops), default=0.0)
            values["Ping Time"] = (
                2e-4 + 2.5e-4 * len(hops) + 4e-3 * path_load + 1e-4 * smooth(f"{node}:ping", t)
            )
            if not up:
                values = {k: v for k, v in values.items() if k.endswith("/Description")}
            out[asset] = values
        return out

    def unit(self, signal: str) -> str | None:
        if signal in UNITS:
            return UNITS[signal]
        return PORT_UNITS.get(signal.rsplit("/", 1)[-1]) if signal.startswith("Ports/") else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "boot_t": dict(self.boot_t),
            "errors": dict(self.errors),
            "was_up": dict(self.was_up),
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        self.boot_t |= {k: float(v) for k, v in s.get("boot_t", {}).items()}
        self.errors = {k: float(v) for k, v in s.get("errors", {}).items()}
        self.was_up = {k: bool(v) for k, v in s.get("was_up", {}).items()}

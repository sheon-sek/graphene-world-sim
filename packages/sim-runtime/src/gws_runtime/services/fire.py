"""Fire detection and protection: detectors, call points, alarm valves, zones and fire pumps.

The cause is a fire in a room (an operating condition: smoke obscuration and hot-layer
temperature) or a person operating a call point (a command). The consequences follow from the
World Model's fire connections, never from a rule about another asset's fault:

- A **smoke detector** alarms while its room's obscuration is at or above its threshold. A
  **heat detector** alarms at its fixed temperature, read against the hotter of the fire's
  hot layer and the room air. A **call point** alarms from the moment it is operated until it
  is reset. A detector fault stops a device from alarming.
- **Sprinklers** open at 68 degC in any room of the zone an **alarm valve** protects, and
  water flows through the valve. Its pressure switch reports the flow after the retard delay,
  unless the switch has failed. A stuck clapper holds the valve where it was.
- A **fire pump** starts `start_delay` after water flows through an alarm valve connected to
  it, runs while it has supply, and stays running until an operator stops it.
- A **zone** goes into alarm `alarm_delay` after any of its devices alarms, and stays in alarm
  (latched, as a fire panel does) until it is reset with none of its devices in alarm. An open
  zone circuit raises no alarm. The zone's alarm reaches what it is connected to: the PAHUs
  that serve its rooms shut down, the lifts return to the ground floor.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.services.common import Env, Signals, number, param
from gws_world_model.model import ROOM_PREFIX, Domain, WorldModel

ZONE = "Fire Zone"
SMOKE = "Smoke Detector"
HEAT = "Heat Detector"
CALL_POINT = "Manual Call Point"
ALARM_VALVE = "Alarm Valve"
FIRE_PUMP = "Fire Pump"
TYPES = frozenset({ZONE, SMOKE, HEAT, CALL_POINT, ALARM_VALVE, FIRE_PUMP})
SPRINKLER_K = 341.15
"""Sprinkler bulb operating temperature (68 degC)."""


@dataclass(frozen=True, slots=True)
class RoomFire:
    """A fire in a room, as its detectors see it."""

    smoke_pct_m: float = 0.0
    """Smoke obscuration, percent per metre."""
    temperature_c: float = 20.0
    """Hot-layer temperature at the ceiling."""


@dataclass
class FireSystem:
    kind: dict[str, str]
    """Asset -> its type id (one of TYPES)."""
    room: dict[str, str | None]
    zone_devices: dict[str, list[str]]
    zone_targets: dict[str, list[str]]
    """Zone -> assets and rooms its alarm reaches (`room:<id>` for a room)."""
    valve_pumps: dict[str, list[str]]
    params: dict[str, dict[str, float]]
    operated: set[str] = field(default_factory=set)
    """Call points a person has operated and nobody has reset."""
    flow_s: dict[str, float] = field(default_factory=dict)
    """Alarm valve -> seconds water has flowed through it."""
    reported: dict[str, bool] = field(default_factory=dict)
    """Alarm valve -> its pressure switch's report."""
    pump_call_s: dict[str, float] = field(default_factory=dict)
    running: set[str] = field(default_factory=set)
    stopped: set[str] = field(default_factory=set)
    """Fire pumps an operator stopped; they start again only on a new flow."""
    device_s: dict[str, float] = field(default_factory=dict)
    """Zone -> seconds any of its devices has been in alarm."""
    latched: set[str] = field(default_factory=set)
    faults: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    alarms: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> FireSystem:
        kind = {a: doc.assets[a].type for a in sorted(scope) if doc.assets[a].type in TYPES}
        zone_devices: dict[str, list[str]] = defaultdict(list)
        zone_targets: dict[str, list[str]] = defaultdict(list)
        valve_pumps: dict[str, list[str]] = defaultdict(list)
        for c in sorted(doc.connections.values(), key=lambda c: c.id):
            if c.domain is not Domain.FIRE or c.source.node not in kind:
                continue
            src, dst = c.source.node, c.target.node
            if kind[src] == ZONE:
                if kind.get(dst) in (SMOKE, HEAT, CALL_POINT, ALARM_VALVE):
                    zone_devices[src].append(dst)
                elif dst.startswith(ROOM_PREFIX) or dst in scope:
                    zone_targets[src].append(
                        dst.split(".", 1)[0] if dst.startswith(ROOM_PREFIX) else dst
                    )
            elif kind[src] == ALARM_VALVE and kind.get(dst) == FIRE_PUMP:
                valve_pumps[src].append(dst)
        names = (
            "alarm_delay",
            "obscuration_threshold",
            "alarm_temp",
            "retard_delay",
            "start_delay",
        )
        params = {
            a: {n: param(doc, a, n, 0.0) for n in names if n in doc.component_types[t].parameters}
            for a, t in kind.items()
        }
        return cls(
            kind=kind,
            room={a: doc.assets[a].location.room for a in kind},
            zone_devices=dict(zone_devices),
            zone_targets=dict(zone_targets),
            valve_pumps=dict(valve_pumps),
            params=params,
        )

    @property
    def assets(self) -> set[str]:
        return set(self.kind)

    @property
    def loads(self) -> set[str]:
        return {a for a, k in self.kind.items() if k == FIRE_PUMP}

    # --- faults and commands -----------------------------------------------------------------

    def fault(self, asset: str, mode: str, values: Mapping[str, float]) -> None:
        if mode == "clapper_stuck":
            values = {**values, "held": float(self.flow_s.get(asset, 0.0) > 0)}
        self.faults.setdefault(asset, {})[mode] = dict(values)

    def clear(self, asset: str, mode: str) -> None:
        self.faults.get(asset, {}).pop(mode, None)

    def command(self, asset: str, signal: str, value: float | bool) -> bool:
        k = self.kind.get(asset)
        if k == CALL_POINT and signal == "operate":
            if value:
                self.operated.add(asset)
            return True
        if signal == "reset" and value:
            if k == CALL_POINT:
                self.operated.discard(asset)
            elif k == ZONE and not self._devices_alarm(asset):
                self.latched.discard(asset)
            return True
        if k == FIRE_PUMP and signal in ("start", "stop"):
            if signal == "stop" and value:
                self.running.discard(asset)
                self.pump_call_s.pop(asset, None)
                self.stopped.add(asset)
            elif signal == "start" and value:
                self.stopped.discard(asset)
                self.pump_call_s.setdefault(asset, 0.0)
            return True
        return False

    def _has(self, asset: str, mode: str) -> bool:
        return mode in self.faults.get(asset, {})

    # --- stepping ----------------------------------------------------------------------------

    def _heat_k(self, room: str | None, env: Env, fires: Mapping[str, RoomFire]) -> float:
        if room is None:
            return 293.15
        air = number(env.state.get(f"{ROOM_PREFIX}{room}", {}), "TAir") or 293.15
        fire = fires.get(room)
        return max(air, fire.temperature_c + 273.15) if fire is not None else air

    def _device_alarm(self, asset: str, env: Env, fires: Mapping[str, RoomFire]) -> bool:
        k, room, p = self.kind[asset], self.room[asset], self.params[asset]
        if self._has(asset, "detector_fault"):
            return False
        if k == SMOKE:
            fire = fires.get(room or "")
            threshold = p.get("obscuration_threshold", 3.0)
            return fire is not None and fire.smoke_pct_m >= threshold
        if k == HEAT:
            return self._heat_k(room, env, fires) >= p.get("alarm_temp", 57.0) + 273.15
        if k == CALL_POINT:
            return asset in self.operated
        if k == ALARM_VALVE:
            return self.reported.get(asset, False)
        return False

    def _devices_alarm(self, zone: str) -> bool:
        return any(self.alarms.get(d, False) for d in self.zone_devices.get(zone, []))

    def _zone_of(self, device: str) -> str | None:
        return next((z for z, ds in self.zone_devices.items() if device in ds), None)

    def step(self, env: Env, fires: Mapping[str, RoomFire]) -> None:
        dt = env.dt
        for valve in (a for a, k in self.kind.items() if k == ALARM_VALVE):
            zone = self._zone_of(valve)
            rooms = {self.room[d] for d in self.zone_devices.get(zone or "", [])} | {
                self.room[valve]
            }
            flowing = any(self._heat_k(r, env, fires) >= SPRINKLER_K for r in rooms if r)
            stuck = self.faults.get(valve, {}).get("clapper_stuck")
            if stuck is not None:
                flowing = bool(stuck.get("held", 0.0))
            self.flow_s[valve] = self.flow_s.get(valve, 0.0) + dt if flowing else 0.0
            delay = self.params[valve].get("retard_delay", 30.0)
            switch_ok = not self._has(valve, "pressure_switch")
            self.reported[valve] = switch_ok and self.flow_s[valve] >= delay
        for pump in (a for a, k in self.kind.items() if k == FIRE_PUMP):
            flow = any(
                self.flow_s.get(v, 0.0) > 0 for v, ps in self.valve_pumps.items() if pump in ps
            )
            if not flow:
                self.stopped.discard(pump)
            if flow and pump not in self.stopped and pump not in self.running:
                self.pump_call_s[pump] = self.pump_call_s.get(pump, 0.0) + dt
            powered = env.supply(pump) >= 0.85
            ok = powered and not self._has(pump, "fail_to_start")
            if self.pump_call_s.get(pump, 0.0) >= self.params[pump].get("start_delay", 5.0) and ok:
                self.running.add(pump)
                self.pump_call_s.pop(pump, None)
            if not ok:
                self.running.discard(pump)
        self.alarms = {
            a: self._device_alarm(a, env, fires)
            for a, k in self.kind.items()
            if k in (SMOKE, HEAT, CALL_POINT, ALARM_VALVE)
        }
        for zone in (a for a, k in self.kind.items() if k == ZONE):
            if self._has(zone, "zone_circuit_fault"):
                self.device_s[zone] = 0.0
                continue
            self.device_s[zone] = (
                self.device_s.get(zone, 0.0) + dt if self._devices_alarm(zone) else 0.0
            )
            if self._devices_alarm(zone) and self.device_s[zone] >= self.params[zone].get(
                "alarm_delay", 0.0
            ):
                self.latched.add(zone)

    # --- outputs -----------------------------------------------------------------------------

    def zone_alarm(self, zone: str) -> bool:
        return zone in self.latched and not self._has(zone, "zone_circuit_fault")

    def reaches(self, target: str) -> bool:
        """Whether a zone in alarm is connected to this asset (or `room:<id>`)."""
        return any(target in ts and self.zone_alarm(z) for z, ts in self.zone_targets.items())

    def demand_w(self, asset: str, rated: float) -> float:
        return rated if asset in self.running else 0.0

    def signals(self) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for a, k in self.kind.items():
            if k == ZONE:
                out[a] = {
                    "alarm": self.zone_alarm(a),
                    "circuit_fault": self._has(a, "zone_circuit_fault"),
                }
            elif k == FIRE_PUMP:
                out[a] = {"running": a in self.running}
            elif k == ALARM_VALVE:
                out[a] = {
                    "operated": self.alarms.get(a, False),
                    "flowing": self.flow_s.get(a, 0.0) > 0,
                }
            elif k == CALL_POINT:
                out[a] = {"activated": self.alarms.get(a, False)}
            else:
                out[a] = {"alarm": self.alarms.get(a, False)}
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "operated": sorted(self.operated),
            "flow_s": dict(self.flow_s),
            "reported": dict(self.reported),
            "pump_call_s": dict(self.pump_call_s),
            "running": sorted(self.running),
            "stopped": sorted(self.stopped),
            "device_s": dict(self.device_s),
            "latched": sorted(self.latched),
            "faults": {a: {m: dict(v) for m, v in f.items()} for a, f in self.faults.items()},
            "alarms": dict(self.alarms),
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        self.operated = set(s["operated"])
        self.flow_s = {k: float(v) for k, v in s["flow_s"].items()}
        self.reported = {k: bool(v) for k, v in s["reported"].items()}
        self.pump_call_s = {k: float(v) for k, v in s["pump_call_s"].items()}
        self.running, self.stopped = set(s["running"]), set(s["stopped"])
        self.device_s = {k: float(v) for k, v in s["device_s"].items()}
        self.latched = set(s["latched"])
        self.faults = {a: {m: dict(v) for m, v in f.items()} for a, f in s["faults"].items()}
        self.alarms = {k: bool(v) for k, v in s["alarms"].items()}

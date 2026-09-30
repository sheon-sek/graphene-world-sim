"""Co-simulation master (ADR-0002): steps every domain of a scope at a fixed macro step.

One macro step from t to t + dt:

1. operating conditions and due auto-clears apply;
2. electrical: every load's demand is last step's (equipment power from the thermofluid
   models, IT load from the conditions), and the network advances and solves at t + dt: the
   electrical side lags the thermal side by one step;
3. thermofluid: each partition's inputs are set — the supply voltage each asset now has, the
   operating conditions, the commands in force, and the effect of each active fault on its
   own asset — and the FMUs integrate to t + dt;
4. site services (cold water, fire, lifts, fuel, room sensors) step on the state the models
   published and the conditions;
5. the true state is assembled, the control network settles and instrumentation measures it;
6. controllers read the measurements and write commands, which act from the next step.

Operator commands (points the World Model marks as commands, ADR-0003) act on the asset's
operator station, not on physics:

- `Auto_Manual` (1 or "Auto", 0 or "Manual"): in manual, controllers no longer write the
  asset's model inputs, so the operator's own commands hold. The point reads 1 or 0.
- `enabled` (Boolean): a permissive. While false, the asset's run input is held off.
- `start` / `stop` (momentary, act on true): switch the asset to manual and set its run input
  (`enable`, `speed`, `fanSpeed` or `position`) on or off.
- `reset` (momentary): a protection reset, as `reset(asset)`.

Everything iterates in sorted order and noise is seeded, so the same World Model, scope,
seed and event sequence always give the same trajectory.

The master contains no rule that makes one asset react to another asset's fault: faults act
only on their own target, and every consequence comes from the models and the controllers.
"""

from __future__ import annotations

import math
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import gws_runtime.controllers.electrical
import gws_runtime.controllers.plant  # noqa: F401 - registers the blocks
from gws_runtime.behaviours import BEHAVIOURS, Behaviour, FaultAction
from gws_runtime.compiler import CACHE, Partition, Plan, compile_partition, ident, plan
from gws_runtime.conditions import Conditions
from gws_runtime.controllers import Block, build
from gws_runtime.controllers.hmi import PlantHmi
from gws_runtime.electrical import ElectricalNetwork
from gws_runtime.faults import Fault, FaultBook
from gws_runtime.fmu import FmuUnit, Scalar
from gws_runtime.instrumentation import Instrumentation
from gws_runtime.netdevices import DeviceTelemetry
from gws_runtime.network import ControlNetwork
from gws_runtime.services import Env, SiteServices
from gws_runtime.values import Sample, Value, split_ref
from gws_world_model.model import (
    ROOM_PREFIX,
    AssetSignal,
    FaultKind,
    WorldModel,
)

del gws_runtime.controllers.electrical  # imported to register its blocks

ON_THRESHOLD = 1e-3
MODE, ENABLED = "Auto_Manual", "enabled"
OPERATOR_SIGNALS = frozenset({MODE, ENABLED, "start", "stop", "reset"})
COMM_LOST = "comm_lost"
"""Alias of a unit's communication-loss alarms: the gateway raises them when the unit cannot
reach it, so they stay fresh while the unit's own readings go stale."""
RUN_INPUTS = ("enable", "speed", "fanSpeed", "position")
"""The model input that runs or stops an asset, by preference."""
"""An output above this counts as running for an `on:<output>` status."""
IT_LOAD_TYPE = "IT Load"
ELECTRICAL_UNITS = {
    "V_pu": "1",
    "V_in_pu": "1",
    "V_normal_pu": "1",
    "V_emergency_pu": "1",
    "P": "W",
    "P_in": "W",
    "P_charge": "W",
    "P_available": "W",
    "demand": "W",
    "Q": "var",
    "Q_in": "var",
    "I": "A",
    "soc": "1",
    "energy": "J",
    "V_ln": "V",
    "V_ll": "V",
    "V_in_ln": "V",
    "V_in_ll": "V",
    "S": "VA",
    "PF": "1",
    "P_ph": "W",
    "Q_ph": "var",
    "S_ph": "VA",
    "I_1ph": "A",
    "Hz": "Hz",
    "THDV": "1",
    "THDA": "1",
    "I_n": "A",
    "I_residual": "A",
    "I_residual_dc": "A",
    "T_coolant": "K",
    "p_oil": "Pa",
    "V_battery": "V",
    "speed": "Hz",
    "run_s": "s",
}

_METER_POINTS = {
    **{f"V{n}": "V_ln" for n in ("1", "2", "3", "sys")},
    **{f"V{n}": "V_ll" for n in ("12", "23", "31", "sys2")},
    **{f"I{n}": "I" for n in ("1", "2", "3", "sys")},
    **{f"P{n}": "P_ph" for n in "123"},
    **{f"Q{n}": "Q_ph" for n in "123"},
    **{f"S{n}": "S_ph" for n in "123"},
    **{f"PF{n}": "PF" for n in ("1", "2", "3", "sys")},
    **{f"THDA{n}": "THDA" for n in "123"},
    **{f"THDV{n}": "THDV" for n in "123"},
    "In": "I_n",
    "Ptot": "P",
    "Qtot": "Q",
    "Stot": "S",
    "Wh_Im": "energy:P",
}
_SINGLE_PHASE_POINTS = {"P1": "P", "Q1": "Q", "S1": "S", "I1": "I_1ph"}
"""A meter with one phase measures a single-phase circuit: its phase is the whole circuit."""
_ELECTRICAL_POINTS = {
    **_METER_POINTS,
    "HasAlarm": "alarm",
    # Branch circuit and residual current monitors.
    "Active Power": "P",
    "Current": "I",
    "Accumulated Energy": "energy:P",
    # Branch circuit monitor totals.
    "Iasys": "I",
    "Wh_Ima": "energy:P",
    # IT load.
    "E": "energy:P",
    # UPS.
    "Average Input Voltage": "V_in_ll",
    **{f"Input Voltage {p}": "V_in_ll" for p in ("L1-L2", "L2-L3", "L3-L1")},
    # The UDT labels its input power members in volts: they read the input phase voltages.
    **{f"Input Power {p}": "V_in_ln" for p in ("L1", "L2", "L3")},
    "Average Output Voltage": "V_ll",
    **{f"Output Voltage {p}": "V_ll" for p in ("L1-L2", "L2-L3", "L3-L1")},
    "Frequency": "Hz",
    "Bypass Undervoltage Warning": "input_low",
    "System Input Power Problem": "input_low",
    "Power Supply Failure": "on_battery",
    "Rectifier Failure": "rectifier_failed",
    "System Output Fault": "output_fault",
    # Genset.
    **{f"AC Voltage: {p}-N": "V_ln" for p in ("L1", "L2", "L3")},
    "Battery DC Volts": "V_battery",
    "Coolant Temperature": "T_coolant",
    "Oil Pressure": "p_oil",
    "Engine Speed": "speed",
    "Engine Run Time": "run_s",
    "Engine Start": "start_cmd",
    "Run Command Active": "start_cmd",
    "Idling": "idling",
    "Emergency Stop": "emergency_stop",
    "General Genset Alarm": "alarm",
    "Genset Prealarm": "prealarm",
    "Low Coolant Level": "low_coolant",
    "Low Lubricant Oil Pressure Prealarm": "oil_prealarm",
    "Low Lubricant Oil Pressure Shutdown": "oil_shutdown",
    "Over Crank Shutdown": "over_crank",
    "Overload Warning": "overload_warning",
    "Short Circuit Shutdown": "short_circuit",
}
"""Point members of electrical assets -> the network's signals (`energy:` integrates)."""


_TYPE_ELECTRICAL_POINTS = {
    # A residual current monitor's current is the circuit's current to earth.
    "RCMS": {"Current": "I_residual"},
    "IPS": {"Insulation Fault": "insulation_fault", "HasAlarm": "insulation_fault"},
}


def electrical_points(members: Collection[str], type_id: str = "") -> dict[str, str]:
    """Point aliases of an electrical asset whose type has these point members."""
    aliases = {m: s for m, s in _ELECTRICAL_POINTS.items() if m in members}
    aliases |= {m: s for m, s in _TYPE_ELECTRICAL_POINTS.get(type_id, {}).items() if m in members}
    aliases |= {m: f"energy:{m[:-6]}_P" for m in members if m.endswith("_Wh_Im")}
    if "P1" in members and "P2" not in members:
        aliases |= {m: s for m, s in _SINGLE_PHASE_POINTS.items() if m in members}
    return aliases


class RuntimeProblem(ValueError):
    """A command, fault or condition the runtime cannot apply."""


@dataclass(frozen=True, slots=True)
class _Input:
    partition: str
    var: str
    kind: str


@dataclass
class Frame:
    """What the runtime publishes after each step."""

    t: float
    step: int
    state: dict[str, dict[str, float | bool | str]]
    """True state of the scope, SI, keyed by asset id (and `room:<id>`)."""
    instruments: dict[str, Sample]
    points: dict[str, Sample]
    faults: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "t": self.t,
            "step": self.step,
            "state": self.state,
            "instruments": {k: _sample(s) for k, s in self.instruments.items()},
            "points": {k: _sample(s) for k, s in self.points.items()},
            "faults": self.faults,
        }


def _sample(s: Sample) -> dict[str, Any]:
    return {"value": s.value, "quality": s.quality.value, "t": s.timestamp, "reason": s.reason}


@dataclass
class _Rebuild:
    params: dict[str, float] = field(default_factory=dict)


class _View:
    """StateView over the assembled true state, with each behaviour's point aliases."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim

    def get(self, asset: str, signal: str) -> float | bool | str | None:
        values = self.sim.state.get(asset)
        if values is None:
            return None
        if signal in (MODE, ENABLED) and self.sim.operable(asset):
            return self.sim.operator_value(asset, signal)
        if self.monitored(asset, signal):
            return not self.sim.network.reachable(asset)
        if signal in values:
            return values[signal]
        alias = self.sim.aliases.get(asset, {}).get(signal)
        if alias is None:
            return None
        if alias.startswith("on:"):
            x = values.get(alias[3:])
            return None if not isinstance(x, float | int) else abs(float(x)) > ON_THRESHOLD
        return values.get(alias)

    def unit(self, asset: str, signal: str) -> str | None:
        alias = self.sim.aliases.get(asset, {}).get(signal, signal)
        if alias.startswith("on:"):
            return None
        return self.sim.units.get((asset, alias)) or ELECTRICAL_UNITS.get(alias)

    def monitored(self, asset: str, signal: str) -> bool:
        return COMM_LOST in (signal, self.sim.aliases.get(asset, {}).get(signal))


class _Clock:
    def __init__(self) -> None:
        self.began = self.last = time.perf_counter()
        self.laps: dict[str, float] = {}

    def lap(self, phase: str) -> None:
        now = time.perf_counter()
        self.laps[phase] = now - self.last
        self.last = now


@dataclass
class StepTimings:
    """Wall-clock seconds per step and per phase (electrical, each partition, instrumentation,
    controllers): the last step, a moving average, and the slowest step so far."""

    steps: int = 0
    last: dict[str, float] = field(default_factory=dict)
    mean: dict[str, float] = field(default_factory=dict)
    slowest: float = 0.0

    def start(self) -> _Clock:
        return _Clock()

    def record(self, clock: _Clock) -> None:
        self.last = {**clock.laps, "total": clock.last - clock.began}
        k = 1.0 if self.steps == 0 else 0.1
        for phase, seconds in self.last.items():
            self.mean[phase] = self.mean.get(phase, seconds) * (1 - k) + seconds * k
        self.slowest = max(self.slowest, self.last["total"])
        self.steps += 1

    def to_json(self) -> dict[str, Any]:
        return {"steps": self.steps, "last": self.last, "mean": self.mean, "slowest": self.slowest}


class Simulation:
    """The runtime for one scope of a World Model revision."""

    def __init__(
        self,
        doc: WorldModel,
        scope: Collection[str],
        *,
        seed: int = 0,
        dt: float = 1.0,
        cache: Path = CACHE,
        start_time: float = 0.0,
        conditions: Conditions | None = None,
    ) -> None:
        self.doc = doc
        self.scope = frozenset(scope)
        self.seed = seed
        self.dt = float(dt)
        self.t = float(start_time)
        self.step_count = 0
        self.cache = cache
        self.plan: Plan = plan(doc, self.scope)
        self.partitions: dict[str, Partition] = {p.name: p for p in self.plan.partitions}
        self.fmu_paths: dict[str, Path] = {}
        self.compile_seconds: dict[str, float] = {}
        """Seconds each partition took to compile when this simulation was built (0: cached)."""
        for name, p in self.partitions.items():
            self.fmu_paths[name], self.compile_seconds[name] = compile_partition(p, cache)
        self.timings = StepTimings()
        self.conditions = conditions or Conditions.from_world(doc.conditions)
        self.electrical = ElectricalNetwork.from_world(doc, self.scope)
        self.services = SiteServices.from_world(doc, self.scope)
        self._services: dict[str, dict[str, float | bool | str]] = {}
        self.network = ControlNetwork.from_world(doc)
        self.telemetry = DeviceTelemetry.from_world(doc, self.network)
        self.instrumentation = Instrumentation(doc, self.network, seed, self.scope)
        self.blocks: list[Block]
        self.blocks, self.missing_blocks = build(doc, self._in_scope)
        self.faults = FaultBook()
        self.commands: dict[str, dict[str, Scalar]] = {}
        """Asset -> model input -> the command in force (operator or controller)."""
        self.register: dict[str, Value] = {}
        """Controller registers written by blocks (`~PLC-01:lead_chiller`) or an operator."""
        self.hmis = {
            c: PlantHmi.build(c, doc, self.blocks)
            for c in sorted({b.binding.controller for b in self.blocks})
            if c in doc.assets and doc.assets[c].type in _CONTROLLERS
        }
        """Controller -> its register table (settings, configuration, status)."""
        for hmi in self.hmis.values():
            self.register |= hmi.defaults()
        self.unrouted: set[str] = set()
        """Writes by blocks to targets outside the scope (reported, not applied)."""
        self.run_hours: dict[str, float] = {}
        self.operator: dict[str, dict[str, Scalar]] = {}
        """Asset -> `Auto_Manual` (1 auto, 0 manual) and `enabled`, where set by an operator."""
        self._frozen: dict[tuple[str, str], Scalar] = {}
        self._applied_sensor: dict[str, dict[str, float]] = {}
        self._applied_electrical: dict[str, dict[str, float]] = {}
        self._applied_services: dict[str, dict[str, float]] = {}
        self._rebuild: dict[str, dict[str, float]] = {}
        self.state: dict[str, dict[str, float | bool | str]] = {}
        self._electrical = self.electrical.signals()

        # What each asset is: its behaviour, inputs, outputs and point aliases.
        self.behaviour: dict[str, Behaviour] = {}
        self.inputs: dict[str, dict[str, _Input]] = {}
        self.outputs: dict[str, dict[str, tuple[str, str]]] = {}
        self.units: dict[tuple[str, str], str] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        for part in self.plan.partitions:
            for v in part.inputs:
                if v.asset is not None:
                    self.inputs.setdefault(v.asset, {})[v.signal] = _Input(
                        part.name, v.name, v.kind
                    )
            for v in part.outputs:
                if v.asset is not None:
                    self.outputs.setdefault(v.asset, {})[v.signal] = (part.name, v.name)
                    self.units[(v.asset, v.signal)] = v.unit
        for asset in sorted(self.plan.modelled):
            b = BEHAVIOURS[doc.component_types[doc.assets[asset].type].behaviour or ""]
            self.behaviour[asset] = b
            self.aliases[asset] = dict(b.points)
        for asset in sorted(self.services.assets):
            if asset not in self.aliases:
                self.aliases[asset] = self.services.points(asset)
        bound: dict[str, set[str]] = {}
        for binding in doc.point_bindings.values():
            if isinstance(binding.source, AssetSignal):
                bound.setdefault(binding.source.asset, set()).add(binding.source.signal)
        for asset in sorted(self._electrical):
            if asset in doc.assets and asset not in self.aliases:
                members = doc.component_types[doc.assets[asset].type].point_template
                self.aliases[asset] = electrical_points(
                    set(members) | bound.get(asset, set()), doc.assets[asset].type
                )
        self._rooms = {room for p in self.plan.partitions for room in p.rooms}
        self.served_room: dict[str, str] = {}
        """Asset -> the room it takes the liquid-cooled IT heat of (a CDU's room connection)."""
        for c in sorted(doc.connections.values(), key=lambda c: c.id):
            served = self.behaviour.get(c.source.node)
            if served is not None and c.target.is_room and c.source.port in served.external:
                if any(spec.liquid_heat for spec in served.inputs.values()):
                    self.served_room[c.source.node] = c.target.room
        self.integrals: dict[str, dict[str, float]] = {}
        """Asset -> signal -> time integral (`energy:<signal>` aliases), SI (J for W)."""
        for asset, aliases in self.aliases.items():
            for alias in aliases.values():
                if alias.startswith("energy:"):
                    self.integrals.setdefault(asset, {})[alias[7:]] = 0.0
        self.it_loads = {
            a.id: a.location.room
            for a in sorted(doc.assets.values(), key=lambda a: a.id)
            if a.id in self.scope and a.type == IT_LOAD_TYPE and a.location.room
        }

        self.fmus: dict[str, FmuUnit] = {}
        for name, part in self.partitions.items():
            self.fmus[name] = FmuUnit(self.fmu_paths[name], part, self.t)
        for name, unit in self.fmus.items():
            for v in self.partitions[name].inputs:
                if v.asset is not None and v.name in unit.input_starts:
                    self.commands.setdefault(v.asset, {})[v.signal] = unit.input_starts[v.name]
        self._services = self.services.signals(self._env(0.0), self.conditions)
        self._publish()

    # --- scope ---------------------------------------------------------------------------

    def _in_scope(self, binding: Any) -> bool:
        """A binding runs when its controller is in scope or it drives something simulated
        (the scope, or the electrical supply path the scope needs)."""
        simulated = self.scope | self.electrical.assets
        assets = {split_ref(r)[0] for r in binding.drives if ":" in r}
        return binding.controller in self.scope or bool(assets & simulated)

    def _asset_kind(self, asset: str) -> str:
        if asset in self.behaviour:
            return "thermofluid"
        if asset in self.services.assets:
            return "services"
        if asset in self.electrical.assets:
            return "electrical"
        return "other"

    # --- commands ------------------------------------------------------------------------

    def command(self, target: str, signal: str | None, value: Scalar | str) -> None:
        """An operator command: `<asset>` + input signal, or a writable point path."""
        if signal is None:
            mapped = self.instrumentation.command_target(target)
            if mapped is None:
                raise RuntimeProblem(f"{target!r} is not a writable command point")
            target, signal = mapped
        if signal in OPERATOR_SIGNALS:
            self._operate(target, signal, value)
            return
        if not self._write(target, signal, value):
            raise RuntimeProblem(f"{target}:{signal} is not a command this scope accepts")

    def _write(self, asset: str, signal: str, value: Value) -> bool:
        if asset in self.hmis and self.hmis[asset].writable(signal):
            self.register[f"{asset}:{signal}"] = value
            self.register[f"{asset}:last_command"] = f"{signal} = {value}"
            return True
        if signal in self.inputs.get(asset, {}):
            if not isinstance(value, bool | int | float):
                raise RuntimeProblem(f"{asset}:{signal} takes a number, not {value!r}")
            kind = self.inputs[asset][signal].kind
            self.commands.setdefault(asset, {})[signal] = (
                bool(value) if kind == "bool" else float(value)
            )
            return True
        if asset in self.services.assets and isinstance(value, bool | int | float):
            if self.services.command(asset, signal, value):
                return True
        if asset in self.electrical.assets and isinstance(value, bool | int | float):
            try:
                self.electrical.command(asset, signal, value)
            except (KeyError, ValueError):
                return False
            return True
        return False

    # --- operator station ------------------------------------------------------------------

    def operable(self, asset: str) -> bool:
        return (
            asset in self.behaviour
            or asset in self.services.assets
            or asset in self.electrical.assets
        )

    def operator_value(self, asset: str, signal: str) -> Scalar:
        default: Scalar = 1 if signal == MODE else True
        return self.operator.get(asset, {}).get(signal, default)

    def manual(self, asset: str) -> bool:
        return self.operator_value(asset, MODE) == 0

    def run_input(self, asset: str) -> str | None:
        inputs = self.inputs.get(asset, {})
        return next((s for s in RUN_INPUTS if s in inputs), None)

    def _operate(self, asset: str, signal: str, value: Value) -> None:
        if not self.operable(asset):
            raise RuntimeProblem(f"{asset} is not simulated in this scope")
        if signal == MODE:
            text = str(value).strip().lower() if isinstance(value, str) else None
            if text in ("auto", "manual"):
                mode = 1 if text == "auto" else 0
            elif isinstance(value, int | float) and not isinstance(value, bool) and value in (0, 1):
                mode = int(value)
            else:
                raise RuntimeProblem(f"{asset}:{MODE} takes 1/Auto or 0/Manual, not {value!r}")
            self.operator.setdefault(asset, {})[MODE] = mode
            if mode == 1 and asset in self.services.assets:
                self.services.command(asset, "auto", True)
            return
        if not isinstance(value, bool | int | float) or isinstance(value, str):
            raise RuntimeProblem(f"{asset}:{signal} takes a Boolean, not {value!r}")
        on = bool(value)
        if signal == ENABLED:
            self.operator.setdefault(asset, {})[ENABLED] = on
            return
        if not on:
            return  # momentary commands act on true
        if signal == "reset":
            self.reset(asset)
            return
        if asset in self.services.assets and self.services.command(asset, signal, True):
            self.operator.setdefault(asset, {})[MODE] = 0
            return
        run = self.run_input(asset)
        if run is None:
            raise RuntimeProblem(f"{asset} has no run command in this scope")
        self.operator.setdefault(asset, {})[MODE] = 0
        self._write(asset, run, signal == "start")

    def reset(self, target: str) -> list[str]:
        """Reset an asset's latched trips (a protection reset). Returns the faults removed."""
        done = self.faults.reset(target)
        for f in done:
            self._unroute(f)
        if target in self.services.assets:
            self.services.command(target, "reset", True)
        if target in self.electrical.assets:
            try:
                self.electrical.command(target, "reset", True)
            except (KeyError, ValueError):
                pass
        return [f.id for f in done]

    # --- faults ----------------------------------------------------------------------------

    def inject(
        self,
        target: str,
        mode: str,
        parameters: Mapping[str, float] | None = None,
        *,
        severity: float = 1.0,
        ramp_s: float = 0.0,
        duration_s: float | None = None,
        fault_id: str = "",
    ) -> Fault:
        """Inject a fault on one target. It acts from the next step."""
        if not 0 <= severity <= 1:
            raise RuntimeProblem("severity must be between 0 and 1")
        params = dict(parameters or {})
        doc = self.doc
        latching = False
        if target in doc.assets:
            spec = doc.component_types[doc.assets[target].type].fault_modes.get(mode)
            if spec is None:
                raise RuntimeProblem(f"{doc.assets[target].type} has no fault mode {mode!r}")
            kind = spec.kind
            defaults = {
                k: float(s.default)
                for k, s in spec.parameters.items()
                if isinstance(s.default, int | float) and not isinstance(s.default, bool)
            }
            unknown = set(params) - set(spec.parameters)
            if unknown:
                raise RuntimeProblem(f"{mode} has no parameters {sorted(unknown)}")
            params = defaults | params
            if self.services.handles(target, mode):
                domain = "services"
                latching = kind is FaultKind.TRIP
            elif kind is FaultKind.SENSOR:
                domain = "sensor"
            elif kind is FaultKind.NETWORK:
                domain = "network"
            elif target in self.behaviour:
                if mode not in self.behaviour[target].faults:
                    raise RuntimeProblem(f"{target} does not model fault mode {mode!r}")
                domain = "thermofluid"
                latching = self.behaviour[target].faults[mode].latching
            elif target in self.electrical.assets:
                domain = "electrical"
                latching = kind is FaultKind.TRIP
            else:
                raise RuntimeProblem(f"{target} is not simulated in this scope")
        elif target in doc.instruments or target in doc.point_bindings:
            kind, domain = FaultKind.SENSOR, "sensor"
        else:
            kind, domain = FaultKind.NETWORK, "network"
            try:
                self.network.fail(target)
                self.network.restore(target)
            except KeyError as e:
                raise RuntimeProblem(str(e)) from e
        fault = Fault(
            id=fault_id,
            target=target,
            mode=mode,
            kind=kind,
            domain=domain,
            parameters=params,
            severity=severity,
            ramp_s=ramp_s,
            duration_s=duration_s,
            start=self.t,
            latching=latching,
        )
        self.faults.add(fault)
        if domain == "thermofluid":
            action = self.behaviour[target].faults[mode]
            for signal in action.freeze:
                value = self.commands.get(target, {}).get(signal)
                if value is not None:
                    self._frozen[(fault.id, signal)] = value
        return fault

    def clear(self, fault_id: str) -> Fault:
        """Clear a fault's cause. A latching trip stays in effect until the target is reset."""
        if fault_id not in self.faults.faults:
            raise RuntimeProblem(f"no active fault {fault_id!r}")
        fault = self.faults.clear(fault_id, self.t)
        if fault_id not in self.faults.faults:
            self._unroute(fault)
        return fault

    def _unroute(self, fault: Fault) -> None:
        """Undo a removed fault's effect in the domain that owns it."""
        for key in [k for k in self._frozen if k[0] == fault.id]:
            del self._frozen[key]
        if fault.domain == "sensor":
            self.instrumentation.clear(fault.target, fault.mode)
            self._applied_sensor.pop(fault.id, None)
        elif fault.domain == "network":
            if not any(
                f.domain == "network" and f.target == fault.target for f in self.faults.active()
            ):
                self.network.restore(fault.target)
        elif fault.domain == "electrical":
            self.electrical.clear(fault.target, fault.mode)
            self._applied_electrical.pop(fault.id, None)
        elif fault.domain == "services":
            self.services.clear(fault.target, fault.mode)
            self._applied_services.pop(fault.id, None)

    def _route_faults(self) -> None:
        """Apply the current value of every non-thermofluid fault to its domain."""
        for f in self.faults.active():
            values = f.values(self.t)
            if f.domain == "sensor":
                if self._applied_sensor.get(f.id) != values:
                    self.instrumentation.fault(f.target, f.mode, values)
                    self._applied_sensor[f.id] = values
            elif f.domain == "network":
                self.network.fail(f.target)
            elif f.domain == "services":
                if self._applied_services.get(f.id) != values:
                    self.services.fault(f.target, f.mode, values)
                    self._applied_services[f.id] = values
            elif f.domain == "electrical" and not f.latched:
                if self._applied_electrical.get(f.id) != values:
                    self.electrical.fault(f.target, f.mode, values)
                    self._applied_electrical[f.id] = values

    def _tripped(self, asset: str) -> bool:
        return any(
            f.target == asset and f.kind is FaultKind.TRIP and f.domain != "sensor"
            for f in self.faults.active()
        )

    # --- conditions ----------------------------------------------------------------------

    def set_conditions(self, changes: Mapping[str, Any]) -> None:
        try:
            self.conditions.set(changes)
        except (KeyError, ValueError) as e:
            raise RuntimeProblem(str(e)) from e

    # --- stepping ------------------------------------------------------------------------

    def _it_demand(self, asset: str) -> float:
        if self._tripped(asset):
            return 0.0  # emergency power off: the IT equipment draws nothing
        return self.conditions.it_demand_w(self.it_loads[asset])

    def _inputs_for(self, name: str) -> dict[str, Scalar]:
        part = self.partitions[name]
        env = self.conditions.environment()
        values: dict[str, Scalar] = {}
        faults = [f for f in self.faults.active() if f.domain == "thermofluid"]
        sensor = [
            f for f in self.faults.active() if f.domain == "sensor" and f.target in self.behaviour
        ]
        for v in part.inputs:
            if v.asset is None:
                values[v.name] = env[v.signal]
                continue
            if v.asset.startswith(ROOM_PREFIX):
                continue
            spec = self.behaviour[v.asset].inputs[v.signal]
            if spec.supply:
                values[v.name] = (
                    self.electrical.supply(v.asset) if v.asset in self.electrical.assets else 1.0
                )
                continue
            if spec.liquid_heat:
                values[v.name] = self._liquid_heat(v.asset)
                continue
            if spec.fire:
                values[v.name] = self.fire_shutdown(v.asset)
                continue
            value = self.commands.get(v.asset, {}).get(v.signal, spec.default)
            if v.signal in RUN_INPUTS and not self.operator_value(v.asset, ENABLED):
                value = False if v.kind == "bool" else 0.0
            for f in faults:
                if f.target != v.asset:
                    continue
                action: FaultAction = self.behaviour[v.asset].faults[f.mode]
                current = f.values(self.t)
                if v.signal in action.override:
                    value = action.override[v.signal]
                elif v.signal in action.freeze and (f.id, v.signal) in self._frozen:
                    value = self._frozen[(f.id, v.signal)]
                for param, signal in action.scale.items():
                    if signal == v.signal and param in current:
                        value = current[param]
            for f in sensor:
                if f.target != v.asset:
                    continue
                action_s = self.behaviour[v.asset].faults.get(f.mode)
                if action_s is not None and v.signal in action_s.offset:
                    param, sign = action_s.offset[v.signal]
                    value = float(value) + sign * f.values(self.t).get(param, 0.0)
            values[v.name] = value
        for room in part.rooms:
            air = 1.0 - self.conditions.liquid_fraction.get(room, 0.0)
            if not any(r == room for r in self.served_room.values()):
                air = 1.0  # no liquid cooling is modelled in the room: all of it heats the air
            q = sum(self._room_heat(asset) for asset, r in self.it_loads.items() if r == room)
            values[f"room_{ident(room)}_QIt"] = q * air
        return values

    def _liquid_heat(self, asset: str) -> float:
        """A CDU's share of the liquid-cooled IT heat of the room it serves."""
        room = self.served_room.get(asset)
        if room is None:
            return 0.0
        units = sum(1 for r in self.served_room.values() if r == room)
        q = sum(self._room_heat(a) for a, r in self.it_loads.items() if r == room)
        return q * self.conditions.liquid_fraction.get(room, 0.0) / units

    def fire_shutdown(self, asset: str) -> bool:
        """Whether a fire zone the asset's `fire` port is connected to is in alarm."""
        return self.services.fire_shutdown(asset)

    def _room_heat(self, asset: str) -> float:
        """Heat the IT load puts into its room: the power it actually draws."""
        if asset in self.electrical.assets:
            p = self._electrical.get(asset, {}).get("P", 0.0)
            return max(float(p), 0.0)
        return self._it_demand(asset)

    def _rebuild_params(self, name: str) -> dict[str, float]:
        """Modelica parameters the active rebuild faults of a partition change."""
        unit = self.fmus[name]
        out: dict[str, float] = {}
        for f in self.faults.active():
            if f.domain != "thermofluid" or f.target not in self.partitions[name].assets:
                continue
            action = self.behaviour[f.target].faults[f.mode]
            current = f.values(self.t)
            for param, (modelica, exponent) in action.rebuild.items():
                fmu_name = f"{ident(f.target)}.{modelica}"
                factor = max(current.get(param, 1.0), 1e-3) ** exponent
                out[fmu_name] = unit.default(fmu_name) * factor
        return out

    def _maybe_rebuild(self, name: str) -> None:
        """Warm rebuild: re-instantiate a partition whose parameter faults changed, carrying
        its physical state and inputs over."""
        wanted = self._rebuild_params(name)
        have = self._rebuild.get(name, {})
        if set(wanted) == set(have) and all(
            math.isclose(wanted[k], have[k], rel_tol=1e-3) for k in wanted
        ):
            return
        old = self.fmus[name]
        part = self.partitions[name]
        params = FmuUnit.start_parameters(part, old.state()) | wanted
        self.fmus[name] = FmuUnit(self.fmu_paths[name], part, self.t, params, dict(old.applied))
        old.close()
        self._rebuild[name] = wanted

    def step(self) -> Frame:
        clock = self.timings.start()
        dt = self.dt
        t_next = self.t + dt
        for f in self.faults.expire(self.t):
            if f.id not in self.faults.faults:
                self._unroute(f)
        self._route_faults()

        # 2. Electrical, from last step's loads.
        self.electrical.set_utility(self.conditions.utility_available)
        for asset in sorted(self.electrical.loads):
            if asset in self.it_loads:
                self.electrical.set_demand(asset, self._it_demand(asset))
            elif asset in self.services.loads:
                self.electrical.set_demand(asset, self.services.demand_w(asset))
            elif asset in self.outputs:
                power = self.behaviour[asset].power
                if power:
                    value = self.state.get(asset, {}).get(power, 0.0)
                    self.electrical.set_demand(asset, max(float(value), 0.0))
        for genset in sorted(self.services.fuel.day):
            if genset in self.electrical.assets:
                self.electrical.set_fuel(genset, self.services.fuel_ok(genset))
        self.electrical.step(t_next, dt)
        self._electrical = self.electrical.signals()
        clock.lap("electrical")

        # 3. Thermofluid.
        for name in sorted(self.fmus):
            self._maybe_rebuild(name)
            self.fmus[name].set_inputs(self._inputs_for(name))
            self.fmus[name].advance(t_next)
            clock.lap(name)

        self.t = t_next
        self.step_count += 1
        # 4. Site services, on the state the models published.
        env = self._env(dt)
        self.services.step(env, self.conditions)
        self._services = self.services.signals(env, self.conditions)
        clock.lap("services")
        # 5. True state, network, instrumentation.
        self._publish(dt)
        clock.lap("instrumentation")
        # 5. Controllers.
        bus = _Bus(self)
        for hmi in self.hmis.values():
            hmi.apply(self.register)
        for blk in self.blocks:
            blk.step(self.t, dt, bus)
        clock.lap("controllers")
        self.timings.record(clock)
        return self.frame()

    def _env(self, dt: float) -> Env:
        def supply(asset: str) -> float:
            return self.electrical.supply(asset) if asset in self.electrical.assets else 1.0

        return Env(self.t, dt, supply, self.state, self.network.reachable)

    def run(self, steps: int) -> Frame:
        frame = self.frame()
        for _ in range(steps):
            frame = self.step()
        return frame

    def _publish(self, dt: float = 0.0) -> None:
        state: dict[str, dict[str, float | bool | str]] = {}
        for asset, values in self._electrical.items():
            state.setdefault(asset, {}).update(values)
        for asset, service in self._services.items():
            state.setdefault(asset, {}).update(service)
            for signal in service:
                if (service_unit := self.services.unit(signal)) is not None:
                    self.units[(asset, signal)] = service_unit
        for asset, device in self.telemetry.signals(self.t, dt, self.state).items():
            state.setdefault(asset, {}).update(device)
            for signal in device:
                if (device_unit := self.telemetry.unit(signal)) is not None:
                    self.units[(asset, signal)] = device_unit
        for name in sorted(self.fmus):
            unit = self.fmus[name]
            out = unit.outputs()
            for v in self.partitions[name].outputs:
                if v.asset is not None:
                    state.setdefault(v.asset, {})[v.signal] = out[v.name]
        for asset, b in self.behaviour.items():
            s = state.setdefault(asset, {})
            s["tripped"] = self._tripped(asset)
            for name, (si_unit, fn) in b.derived.items():
                try:
                    s[name] = fn(s)  # type: ignore[arg-type]
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    continue
                if si_unit is not None:
                    self.units[(asset, name)] = si_unit
            s["alarm"] = bool(s["tripped"]) or any(s.get(a) is True for a in b.alarms)
        for asset, signals in self.integrals.items():
            s = state.setdefault(asset, {})
            for signal in signals:
                x = s.get(signal)
                if isinstance(x, int | float) and not isinstance(x, bool):
                    signals[signal] += float(x) * dt
                s[f"energy:{signal}"] = signals[signal]
                self.units[(asset, f"energy:{signal}")] = "J"
        for asset in self.it_loads:
            s = state.setdefault(asset, {})
            s["tripped"] = self._tripped(asset)
            s.setdefault("demand", self._it_demand(asset))
        for blk in self.blocks:
            for k, x in blk.signals().items():
                state.setdefault(blk.binding.controller, {})[f"{blk.function}.{k}"] = x
        for controller, hmi in self.hmis.items():
            lead = self.register.get(f"{controller}:lead_chiller")
            state.setdefault(controller, {}).update(hmi.signals(self.t, state, lead))
        for reference, value in self.register.items():
            asset, signal = split_ref(reference)
            if isinstance(value, int | float | bool | str):
                state.setdefault(asset, {})[signal] = value
        self.state = state
        view = _View(self)
        for asset in self.behaviour:
            if "run_hours" in self.aliases[asset].values():
                running = view.get(asset, "On_Off")
                hours = self.run_hours.get(asset, 0.0) + (dt / 3600 if running else 0.0)
                self.run_hours[asset] = hours
                state[asset]["run_hours"] = hours
        self.network.step(self.t)
        self.instrumentation.update(self.t, view)

    # --- output --------------------------------------------------------------------------

    def _scoped_points(self) -> list[str]:
        return self.instrumentation.paths

    def frame(self) -> Frame:
        points = self.instrumentation.points()
        return Frame(
            t=self.t,
            step=self.step_count,
            state={
                k: dict(v)
                for k, v in sorted(self.state.items())
                if k in self.scope or k.startswith("room:")
            },
            instruments={
                i: self.instrumentation.reading(i)
                for i in sorted(self.doc.instruments)
                if self.doc.instruments[i].asset in self.scope
            },
            points={p: points[p] for p in self._scoped_points() if p in points},
            faults=[f.to_json() for f in self.faults.active()],
        )

    # --- lifecycle -----------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Everything needed to continue from now: JSON-able, keyed by World Model ids."""
        return {
            "t": self.t,
            "step": self.step_count,
            "seed": self.seed,
            "dt": self.dt,
            "thermofluid": {
                owner: params
                for name in sorted(self.fmus)
                for owner, params in self.fmus[name].state().items()
            },
            "commands": {a: dict(c) for a, c in self.commands.items()},
            "register": dict(self.register),
            "run_hours": dict(self.run_hours),
            "integrals": {a: dict(x) for a, x in self.integrals.items()},
            "operator": {a: dict(o) for a, o in self.operator.items()},
            "frozen": [[k[0], k[1], v] for k, v in self._frozen.items()],
            "conditions": self.conditions.snapshot(),
            "faults": self.faults.snapshot(),
            "electrical": self.electrical.snapshot(),
            "services": self.services.snapshot(),
            "network": self.network.snapshot(),
            "telemetry": self.telemetry.snapshot(),
            "instrumentation": self.instrumentation.snapshot(),
            "blocks": {b.binding.id: b.snapshot() for b in self.blocks},
            "hmi": {c: h.snapshot() for c, h in self.hmis.items()},
        }

    def restore(self, snap: Mapping[str, Any]) -> None:
        """Continue from a snapshot (of this or an earlier revision of the World Model): state
        is matched by asset id; anything the snapshot lacks keeps its start value."""
        self.t = float(snap["t"])
        self.step_count = int(snap["step"])
        self.commands = {a: dict(c) for a, c in snap["commands"].items() if a in self.inputs}
        self.register = {k: v for h in self.hmis.values() for k, v in h.defaults().items()}
        self.register |= snap["register"]
        for controller, hmi in self.hmis.items():
            hmi.restore(snap.get("hmi", {}).get(controller, {}))
        self.run_hours = {a: float(h) for a, h in snap["run_hours"].items()}
        for asset, signals in snap.get("integrals", {}).items():
            for signal, value in signals.items():
                if signal in self.integrals.get(asset, {}):
                    self.integrals[asset][signal] = float(value)
        self.operator = {a: dict(o) for a, o in snap.get("operator", {}).items()}
        self._frozen = {(k[0], k[1]): k[2] for k in snap["frozen"]}
        self.conditions = Conditions.from_snapshot(snap["conditions"])
        self.faults.restore(snap["faults"])
        self._applied_sensor, self._applied_electrical, self._rebuild = {}, {}, {}
        self._applied_services = {}
        self.electrical.restore(snap["electrical"])
        if "services" in snap:
            self.services.restore(snap["services"])
        self.network.restore_state(snap["network"])
        self.telemetry.restore(snap.get("telemetry", {}))
        self.instrumentation.restore(snap["instrumentation"])
        for b in self.blocks:
            if b.binding.id in snap["blocks"]:
                b.restore(snap["blocks"][b.binding.id])
        self._route_faults()
        thermo = snap["thermofluid"]
        for name, part in self.partitions.items():
            self.fmus[name].close()
            inputs = {
                v.name: self.commands[v.asset][v.signal]
                for v in part.inputs
                if v.asset in self.commands and v.signal in self.commands[v.asset]
            }
            self._rebuild[name] = {}
            self.fmus[name] = FmuUnit(
                self.fmu_paths[name],
                part,
                self.t,
                FmuUnit.start_parameters(part, thermo),
                inputs,
            )
            self._maybe_rebuild(name)
        self._services = self.services.signals(self._env(0.0), self.conditions)
        self._publish()

    def close(self) -> None:
        for unit in self.fmus.values():
            unit.close()
        self.fmus = {}


class _Bus:
    """SignalBus for the controller blocks: measured values in, commands out."""

    def __init__(self, sim: Simulation) -> None:
        self.sim = sim

    def read(self, reference: str) -> Value:
        if reference in self.sim.register:
            return self.sim.register[reference]
        try:
            return self.sim.instrumentation.read(reference)
        except (KeyError, ValueError):
            return None

    def write(self, reference: str, value: Value) -> None:
        asset, signal = split_ref(reference)
        simulated = asset in self.sim.scope or asset in self.sim.electrical.assets
        if simulated and self.sim.manual(asset) and signal in self.sim.inputs.get(asset, {}):
            return  # in manual, the operator's command holds
        if simulated and self.sim._write(asset, signal, value):
            return
        doc = self.sim.doc
        if asset in doc.assets and doc.assets[asset].type in _CONTROLLERS:
            self.sim.register[reference] = value
        else:
            self.sim.unrouted.add(reference)


_CONTROLLERS = frozenset({"Chiller Plant Controller"})
"""Types whose command signals are registers of the controller itself."""

"""Co-simulation master (ADR-0002): steps every domain of a scope at a fixed macro step.

One macro step from t to t + dt:

1. operating conditions and due auto-clears apply;
2. electrical: every load's demand is last step's (equipment power from the thermofluid
   models, IT load from the conditions), and the network advances and solves at t + dt: the
   electrical side lags the thermal side by one step;
3. thermofluid: each partition's inputs are set — the supply voltage each asset now has, the
   operating conditions, the commands in force, and the effect of each active fault on its
   own asset — and the FMUs integrate to t + dt;
4. the true state is assembled, the control network settles and instrumentation measures it;
5. controllers read the measurements and write commands, which act from the next step.

Everything iterates in sorted order and noise is seeded, so the same World Model, scope,
seed and event sequence always give the same trajectory.

The master contains no rule that makes one asset react to another asset's fault: faults act
only on their own target, and every consequence comes from the models and the controllers.
"""

from __future__ import annotations

import math
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
from gws_runtime.electrical import ElectricalNetwork
from gws_runtime.faults import Fault, FaultBook
from gws_runtime.fmu import FmuUnit, Scalar
from gws_runtime.instrumentation import Instrumentation
from gws_runtime.network import ControlNetwork
from gws_runtime.values import Sample, Value, split_ref
from gws_world_model.model import (
    ROOM_PREFIX,
    AssetSignal,
    FaultKind,
    InstrumentSource,
    WorldModel,
)

del gws_runtime.controllers.electrical  # imported to register its blocks

ON_THRESHOLD = 1e-3
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
}


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

    def get(self, asset: str, signal: str) -> float | bool | None:
        values = self.sim.state.get(asset)
        if values is None:
            return None
        if signal in values:
            v = values[signal]
            return v if isinstance(v, float | bool | int) else None
        alias = self.sim.aliases.get(asset, {}).get(signal)
        if alias is None:
            return None
        if alias.startswith("on:"):
            x = values.get(alias[3:])
            return None if not isinstance(x, float | int) else abs(float(x)) > ON_THRESHOLD
        found = values.get(alias)
        return found if isinstance(found, float | bool | int) else None

    def unit(self, asset: str, signal: str) -> str | None:
        alias = self.sim.aliases.get(asset, {}).get(signal, signal)
        if alias.startswith("on:"):
            return None
        return self.sim.units.get((asset, alias)) or ELECTRICAL_UNITS.get(alias)


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
        self.fmu_paths = {
            name: compile_partition(p, cache)[0] for name, p in self.partitions.items()
        }
        self.conditions = conditions or Conditions.from_world(doc.conditions)
        self.electrical = ElectricalNetwork.from_world(doc, self.scope)
        self.network = ControlNetwork.from_world(doc)
        self.instrumentation = Instrumentation(doc, self.network, seed, self.scope)
        self.blocks: list[Block]
        self.blocks, self.missing_blocks = build(doc, self._in_scope)
        self.faults = FaultBook()
        self.commands: dict[str, dict[str, Scalar]] = {}
        """Asset -> model input -> the command in force (operator or controller)."""
        self.register: dict[str, Value] = {}
        """Controller registers written by blocks (`~PLC-01:lead_chiller`)."""
        self.unrouted: set[str] = set()
        """Writes by blocks to targets outside the scope (reported, not applied)."""
        self.run_hours: dict[str, float] = {}
        self._frozen: dict[tuple[str, str], Scalar] = {}
        self._applied_sensor: dict[str, dict[str, float]] = {}
        self._applied_electrical: dict[str, dict[str, float]] = {}
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
        self._rooms = {room for p in self.plan.partitions for room in p.rooms}
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
        if not self._write(target, signal, value):
            raise RuntimeProblem(f"{target}:{signal} is not a command this scope accepts")

    def _write(self, asset: str, signal: str, value: Value) -> bool:
        if signal in self.inputs.get(asset, {}):
            if not isinstance(value, bool | int | float):
                raise RuntimeProblem(f"{asset}:{signal} takes a number, not {value!r}")
            kind = self.inputs[asset][signal].kind
            self.commands.setdefault(asset, {})[signal] = (
                bool(value) if kind == "bool" else float(value)
            )
            return True
        if asset in self.electrical.assets and isinstance(value, bool | int | float):
            try:
                self.electrical.command(asset, signal, value)
            except (KeyError, ValueError):
                return False
            return True
        return False

    def reset(self, target: str) -> list[str]:
        """Reset an asset's latched trips (a protection reset). Returns the faults removed."""
        done = self.faults.reset(target)
        for f in done:
            self._unroute(f)
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
            if kind is FaultKind.SENSOR:
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
            value = self.commands.get(v.asset, {}).get(v.signal, spec.default)
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
            q = sum(self._room_heat(asset) for asset, r in self.it_loads.items() if r == room)
            values[f"room_{ident(room)}_QIt"] = q
        return values

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
            elif asset in self.outputs:
                power = self.behaviour[asset].power
                if power:
                    value = self.state.get(asset, {}).get(power, 0.0)
                    self.electrical.set_demand(asset, max(float(value), 0.0))
        self.electrical.step(t_next, dt)
        self._electrical = self.electrical.signals()

        # 3. Thermofluid.
        for name in sorted(self.fmus):
            self._maybe_rebuild(name)
            self.fmus[name].set_inputs(self._inputs_for(name))
            self.fmus[name].advance(t_next)

        self.t = t_next
        self.step_count += 1
        # 4. True state, network, instrumentation.
        self._publish(dt)
        # 5. Controllers.
        bus = _Bus(self)
        for blk in self.blocks:
            blk.step(self.t, dt, bus)
        return self.frame()

    def run(self, steps: int) -> Frame:
        frame = self.frame()
        for _ in range(steps):
            frame = self.step()
        return frame

    def _publish(self, dt: float = 0.0) -> None:
        state: dict[str, dict[str, float | bool | str]] = {}
        for asset, values in self._electrical.items():
            state.setdefault(asset, {}).update(values)
        for name in sorted(self.fmus):
            unit = self.fmus[name]
            out = unit.outputs()
            for v in self.partitions[name].outputs:
                if v.asset is not None:
                    state.setdefault(v.asset, {})[v.signal] = out[v.name]
        for asset in self.behaviour:
            s = state.setdefault(asset, {})
            s["tripped"] = self._tripped(asset)
        for asset in self.it_loads:
            s = state.setdefault(asset, {})
            s["tripped"] = self._tripped(asset)
            s.setdefault("demand", self._it_demand(asset))
        for blk in self.blocks:
            for k, x in blk.signals().items():
                state.setdefault(blk.binding.controller, {})[f"{blk.function}.{k}"] = x
        for reference, value in self.register.items():
            asset, signal = split_ref(reference)
            if isinstance(value, float | bool | str):
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
        if not hasattr(self, "_points"):
            keep = []
            for path, b in self.doc.point_bindings.items():
                src = b.source
                if isinstance(src, AssetSignal) and src.asset in self.scope:
                    keep.append(path)
                elif (
                    isinstance(src, InstrumentSource)
                    and self.doc.instruments[src.instrument].asset in self.scope
                ):
                    keep.append(path)
            self._points = sorted(keep)
        return self._points

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
            "frozen": [[k[0], k[1], v] for k, v in self._frozen.items()],
            "conditions": self.conditions.snapshot(),
            "faults": self.faults.snapshot(),
            "electrical": self.electrical.snapshot(),
            "network": self.network.snapshot(),
            "instrumentation": self.instrumentation.snapshot(),
            "blocks": {b.binding.id: b.snapshot() for b in self.blocks},
        }

    def restore(self, snap: Mapping[str, Any]) -> None:
        """Continue from a snapshot (of this or an earlier revision of the World Model): state
        is matched by asset id; anything the snapshot lacks keeps its start value."""
        self.t = float(snap["t"])
        self.step_count = int(snap["step"])
        self.commands = {a: dict(c) for a, c in snap["commands"].items() if a in self.inputs}
        self.register = dict(snap["register"])
        self.run_hours = {a: float(h) for a, h in snap["run_hours"].items()}
        self._frozen = {(k[0], k[1]): k[2] for k in snap["frozen"]}
        self.conditions = Conditions.from_snapshot(snap["conditions"])
        self.faults.restore(snap["faults"])
        self._applied_sensor, self._applied_electrical, self._rebuild = {}, {}, {}
        self.electrical.restore(snap["electrical"])
        self.network.restore_state(snap["network"])
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
        if simulated and self.sim._write(asset, signal, value):
            return
        doc = self.sim.doc
        if asset in doc.assets and doc.assets[asset].type in _CONTROLLERS:
            self.sim.register[reference] = value
        else:
            self.sim.unrouted.add(reference)


_CONTROLLERS = frozenset({"Chiller Plant Controller"})
"""Types whose command signals are registers of the controller itself."""

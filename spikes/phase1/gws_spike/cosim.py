"""Co-simulation master for the spike: one thermofluid FMU plus a pandapower network.

Each macro step:
1. read every asset's electrical demand from the FMU and load the pandapower buses,
2. solve the power flow (breaker states included),
3. write each powered asset's bus voltage into its ``V_pu`` input,
4. advance the thermofluid model by one step.

The FMU runs as Model Exchange under FMPy's CVODE, not as OpenModelica's Co-Simulation
wrapper: in OpenModelica 1.25 the Co-Simulation doStep leaks about 11 kB per call and aborts
after a few hundred thousand steps (see the spike report).

The electrical side lags the thermal side by one step (explicit Gauss-Seidel coupling).
Faults act on exactly one asset or one breaker; nothing here knows what any other asset
will do in response.
"""

from __future__ import annotations

import math
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import pandapower as pp
from fmpy import extract, read_model_description
from fmpy.fmi2 import FMU2Model
from fmpy.sundials import CVodeSolver

from gws_spike.generate import TYPES, ident

LOAD_DEADBAND = 0.01

# What a protection trip does to an asset of each ComponentType: the asset stops acting on
# its command. This is a property of the asset's own type, never of its neighbours.
TRIP: dict[str, tuple[str, Any]] = {
    "Chiller": ("enable", False),
    "Chiller Pump": ("speed", 0.0),
    "Cooling Tower": ("fanSpeed", 0.0),
    "FCU": ("fanSpeed", 0.0),
    "Chiller Valve": ("position", 0.0),  # a failed-closed actuator
}


def build_network(spec: dict[str, Any]) -> tuple[pp.pandapowerNet, dict[str, int], dict[str, int]]:
    net = pp.create_empty_network()
    buses = {spec["grid"]["bus"]: pp.create_bus(net, vn_kv=spec["grid"]["vn_kv"], name="MV")}
    pp.create_ext_grid(net, buses[spec["grid"]["bus"]], vm_pu=1.0)
    for b in spec["buses"]:
        buses[b["id"]] = pp.create_bus(net, vn_kv=b["vn_kv"], name=b["id"])
    for t in spec["transformers"]:
        pp.create_transformer_from_parameters(
            net,
            buses[t["hv"]],
            buses[t["lv"]],
            sn_mva=t["sn_mva"],
            vn_hv_kv=net.bus.vn_kv[buses[t["hv"]]],
            vn_lv_kv=net.bus.vn_kv[buses[t["lv"]]],
            vk_percent=6.0,
            vkr_percent=1.0,
            pfe_kw=2.0,
            i0_percent=0.2,
            name=t["id"],
        )
    breakers: dict[str, int] = {}
    for f in spec["feeders"]:
        line = pp.create_line(
            net,
            buses[f["from"]],
            buses[f["to"]],
            length_km=f["length_km"],
            std_type="NAYY 4x150 SE",
            parallel=f["parallel"],
            name=f["id"],
        )
        breakers[f["id"]] = pp.create_switch(
            net, buses[f["from"]], line, et="l", closed=True, name=f["id"]
        )
    return net, buses, breakers


class _NoInput:
    """Inputs are written directly between steps and are constant within a step."""

    def apply(self, *args: Any, **kwargs: Any) -> None:
        pass

    def nextEvent(self, time: float) -> float:  # noqa: N802 - FMPy's interface
        return math.inf


class _Integrator:
    """Integrates a Model Exchange FMU between communication points, handling events."""

    def __init__(self, fmu: FMU2Model, md: Any, start_time: float) -> None:
        self.fmu = fmu
        self.needs_completed = md.modelExchange.needsCompletedIntegratorStep
        self.solver = CVodeSolver(
            nx=md.numberOfContinuousStates,
            nz=md.numberOfEventIndicators,
            get_x=fmu.getContinuousStates,
            set_x=fmu.setContinuousStates,
            get_dx=fmu.getDerivatives,
            get_z=fmu.getEventIndicators,
            get_nominals=fmu.getNominalsOfContinuousStates,
            set_time=fmu.setTime,
            input=_NoInput(),
            startTime=start_time,
            maxStep=60.0,
            relativeTolerance=1e-6,
        )
        self.next_time_event = math.inf

    def _event_iteration(self) -> None:
        more = True
        while more:
            more, terminate, _, _, defined, next_time = self.fmu.newDiscreteStates()
            if terminate:
                raise RuntimeError("model requested termination")
        self.next_time_event = next_time if defined else math.inf

    def initialise(self) -> None:
        self._event_iteration()
        self.fmu.enterContinuousTimeMode()

    def discontinuity(self, time: float, write: Callable[[], None] | None = None) -> None:
        """An event at ``time``: apply ``write`` (input changes; FMI 2 only accepts discrete
        inputs in Event Mode), settle discrete states and restart the solver."""
        self.fmu.enterEventMode()
        if write is not None:
            write()
        self._event_iteration()
        self.fmu.enterContinuousTimeMode()
        self.solver.reset(time)

    def advance(self, time: float, until: float) -> float:
        while time < until - 1e-9:
            target = min(until, self.next_time_event)
            state_event, _, time = self.solver.step(time, target)
            self.fmu.setTime(time)
            step_event = False
            if self.needs_completed:
                step_event, terminate = self.fmu.completedIntegratorStep()
                if terminate:
                    raise RuntimeError("model requested termination")
            time_event = abs(time - self.next_time_event) < 1e-9
            if state_event or step_event or time_event:
                self.discontinuity(time)
        return time


@dataclass
class Fault:
    target: str
    kind: str  # "trip" (an asset) or "open" (a breaker)


class Plant:
    def __init__(
        self,
        fmu_path: str,
        point_map: dict[str, Any],
        fragment: dict[str, Any],
        start_params: dict[str, dict[str, float]] | None = None,
        start_time: float = 0.0,
    ) -> None:
        self.map = point_map
        self.types = {a["id"]: a["type"] for a in fragment["assets"]}
        md = read_model_description(fmu_path)
        self._dir = extract(fmu_path)
        self.vr = {v.name: v.valueReference for v in md.modelVariables}
        self.bools = {v.name for v in md.modelVariables if v.type == "Boolean"}
        settable = {
            v.name
            for v in md.modelVariables
            if v.causality == "parameter" and v.variability in ("fixed", "tunable")
        }
        self.fmu = FMU2Model(
            guid=md.guid,
            unzipDirectory=self._dir,
            modelIdentifier=md.modelExchange.modelIdentifier,
            instanceName=point_map["model"],
        )
        self.fmu.instantiate(loggingOn=False)
        self.fmu.setupExperiment(startTime=start_time)
        for asset_id, params in (start_params or {}).items():
            for p, value in params.items():
                name = f"{ident(asset_id)}.{p}"
                if name not in self.vr:
                    continue  # asset or quantity absent from this model: defaults apply
                if name not in settable:
                    raise ValueError(f"{name} is not a settable parameter in this FMU")
                self.fmu.setReal([self.vr[name]], [value])
        self.fmu.enterInitializationMode()
        self.fmu.exitInitializationMode()
        self._integrator = _Integrator(self.fmu, md, start_time)
        self._integrator.initialise()
        self._applied: dict[str, Any] = {}
        self.time = start_time

        spec = fragment["electrical"]
        self.net, self.buses, self.breakers = build_network(spec)
        self.pf = spec["power_factor"]
        self.load_idx = {
            asset: pp.create_load(self.net, self.buses[bus], p_mw=0.0, name=asset)
            for asset, bus in spec["loads"].items()
        }
        self.bus_of = spec["loads"]
        self.commands = {k: v["start"] for k, v in point_map["inputs"].items()}
        self.faults: list[Fault] = []
        self.pf_seconds = 0.0
        self.pf_solves = 0
        self.volts: dict[str, float] = {}

    # --- signals -------------------------------------------------------------------------
    def _write(self, name: str, value: Any) -> None:
        if name in self.bools:
            self.fmu.setBoolean([self.vr[name]], [bool(value)])
        else:
            self.fmu.setReal([self.vr[name]], [float(value)])

    def read(self, names: Iterable[str]) -> dict[str, float]:
        out = {}
        for n in names:
            if n in self.bools:
                out[n] = float(self.fmu.getBoolean([self.vr[n]])[0])
            else:
                out[n] = self.fmu.getReal([self.vr[n]])[0]
        return out

    def command(self, asset_id: str, signal: str, value: Any) -> None:
        self.commands[f"{ident(asset_id)}_{signal}"] = value

    # --- faults (causes) -----------------------------------------------------------------
    def inject(self, fault: Fault) -> None:
        self.faults.append(fault)

    def clear(self, target: str) -> None:
        self.faults = [f for f in self.faults if f.target != target]

    def _effective_inputs(self) -> dict[str, Any]:
        values = dict(self.commands)
        for f in self.faults:
            if f.kind == "trip":
                signal, value = TRIP[self.types[f.target]]
                values[f"{ident(f.target)}_{signal}"] = value
        return values

    # --- stepping ------------------------------------------------------------------------
    def _solve_electrical(self) -> dict[str, float]:
        """Quasi-static power flow, re-solved only when breakers or loads have changed.

        A load that moved by less than ``LOAD_DEADBAND`` of its last solved value, with no
        breaker change, leaves the voltages as they were.
        """
        power = self.read(self.map["power"].values())
        demand = {asset: max(power[var], 0.0) / 1e6 for asset, var in self.map["power"].items()}
        open_breakers = frozenset(f.target for f in self.faults if f.kind == "open")
        last = getattr(self, "_last", None)
        if (
            last is not None
            and last[0] == open_breakers
            and all(
                abs(demand[a] - last[1][a]) <= LOAD_DEADBAND * max(last[1][a], 1e-3) for a in demand
            )
        ):
            return self.volts
        q_ratio = math.tan(math.acos(self.pf))
        for asset, p_mw in demand.items():
            self.net.load.at[self.load_idx[asset], "p_mw"] = p_mw
            self.net.load.at[self.load_idx[asset], "q_mvar"] = p_mw * q_ratio
        for name, idx in self.breakers.items():
            self.net.switch.at[idx, "closed"] = name not in open_breakers
        t0 = time.perf_counter()
        pp.runpp(self.net, numba=False)
        self.pf_seconds += time.perf_counter() - t0
        self.pf_solves += 1
        self._last = (open_breakers, demand)
        vm = self.net.res_bus.vm_pu
        return {b: (0.0 if math.isnan(vm[i]) else float(vm[i])) for b, i in self.buses.items()}

    def step(self, dt: float = 1.0) -> None:
        volts = self._solve_electrical()
        values = self._effective_inputs()
        for asset, bus in self.bus_of.items():
            if TYPES[self.types[asset]].motor:
                values[f"{ident(asset)}_V_pu"] = volts[bus]
        changed = {k: v for k, v in values.items() if self._applied.get(k) != v}
        if changed:

            def write() -> None:
                for name, value in changed.items():
                    self._write(name, value)

            self._integrator.discontinuity(self.time, write)
            self._applied.update(changed)
        self.time = self._integrator.advance(self.time, self.time + dt)
        self.volts = volts

    # --- state transfer ------------------------------------------------------------------
    def snapshot(self) -> dict[str, dict[str, float]]:
        """Physical state per World Model asset, as start parameters for a rebuilt model."""
        return {
            asset: {p: self.fmu.getReal([self.vr[v]])[0] for p, v in params.items()}
            for asset, params in self.map["state"].items()
        }

    def close(self) -> None:
        self.fmu.terminate()
        self.fmu.freeInstance()
        shutil.rmtree(self._dir, ignore_errors=True)

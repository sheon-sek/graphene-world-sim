"""One compiled partition running as a Model Exchange FMU under FMPy's CVODE (ADR-0002).

OpenModelica 1.25's Co-Simulation wrapper leaks memory on every step, so the runtime
integrates the Model Exchange interface itself. Inputs are constant within a macro step and
written at its start as an event, because FMI 2 only accepts discrete (Boolean) inputs in
Event Mode.

State carries across a re-instantiation (a warm rebuild for a parameter fault, a restore) as
literal start parameters: the partition's `state` map says which variable holds each one and
its `start` map which parameter takes it back.
"""

from __future__ import annotations

import math
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from fmpy import extract, read_model_description
from fmpy.fmi2 import FMU2Model
from fmpy.sundials import CVodeSolver

from gws_runtime.compiler import Partition

type Scalar = float | bool
MAX_SOLVER_STEP_S = 60.0
RELATIVE_TOLERANCE = 1e-6


class _NoInput:
    """Inputs are written between steps and are constant within one."""

    def apply(self, *args: Any, **kwargs: Any) -> None:
        pass

    def nextEvent(self, time: float) -> float:  # noqa: N802 - FMPy's interface
        return math.inf


class FmuUnit:
    """An instance of one partition's FMU, from `start_time` onwards."""

    def __init__(
        self,
        path: Path,
        partition: Partition,
        start_time: float,
        parameters: Mapping[str, float] | None = None,
        inputs: Mapping[str, Scalar] | None = None,
    ) -> None:
        self.partition = partition
        self.path = path
        self.solver_steps = 0
        """Integrator steps taken so far, and the events (state, step or time) handled."""
        self.events = 0
        md = read_model_description(str(path))
        self._dir = extract(str(path))
        self._vr = {v.name: v.valueReference for v in md.modelVariables}
        self._bools = {v.name for v in md.modelVariables if v.type == "Boolean"}
        self._defaults = {
            v.name: float(v.start)
            for v in md.modelVariables
            if v.causality == "parameter" and v.type == "Real" and v.start is not None
        }
        self._settable = {
            v.name
            for v in md.modelVariables
            if v.causality == "parameter" and v.variability in ("fixed", "tunable")
        }
        self._outputs = [v.name for v in partition.outputs]
        self.input_starts: dict[str, Scalar] = {
            v.name: (str(v.start).lower() in ("true", "1"))
            if v.type == "Boolean"
            else float(v.start)
            for v in md.modelVariables
            if v.causality == "input" and v.start is not None
        }
        """Each input's compiled start value: the command before anything writes one."""
        self.fmu = FMU2Model(
            guid=md.guid,
            unzipDirectory=self._dir,
            modelIdentifier=md.modelExchange.modelIdentifier,
            instanceName=partition.name,
        )
        self.fmu.instantiate(loggingOn=False)
        self.fmu.setupExperiment(startTime=start_time)
        for name, value in (parameters or {}).items():
            if name not in self._settable:
                raise ValueError(f"{name} is not a settable parameter of {partition.name}")
            self.fmu.setReal([self._vr[name]], [float(value)])
        self.applied: dict[str, Scalar] = {}
        for name, value in (inputs or {}).items():
            self._write(name, value)
            self.applied[name] = value
        self.fmu.enterInitializationMode()
        self.fmu.exitInitializationMode()
        self._needs_completed = md.modelExchange.needsCompletedIntegratorStep
        self._solver = CVodeSolver(
            nx=md.numberOfContinuousStates,
            nz=md.numberOfEventIndicators,
            get_x=self.fmu.getContinuousStates,
            set_x=self.fmu.setContinuousStates,
            get_dx=self.fmu.getDerivatives,
            get_z=self.fmu.getEventIndicators,
            get_nominals=self.fmu.getNominalsOfContinuousStates,
            set_time=self.fmu.setTime,
            input=_NoInput(),
            startTime=start_time,
            maxStep=MAX_SOLVER_STEP_S,
            relativeTolerance=RELATIVE_TOLERANCE,
        )
        self._next_event = math.inf
        self._event_iteration()
        self.fmu.enterContinuousTimeMode()
        self.time = start_time

    # --- values ----------------------------------------------------------------------------

    def default(self, parameter: str) -> float:
        """A parameter's compiled value, which a rebuild fault scales."""
        return self._defaults[parameter]

    def _write(self, name: str, value: Scalar) -> None:
        if name in self._bools:
            self.fmu.setBoolean([self._vr[name]], [bool(value)])
        else:
            self.fmu.setReal([self._vr[name]], [float(value)])

    def read(self, names: Iterable[str]) -> dict[str, Scalar]:
        out: dict[str, Scalar] = {}
        for n in names:
            if n in self._bools:
                out[n] = bool(self.fmu.getBoolean([self._vr[n]])[0])
            else:
                out[n] = float(self.fmu.getReal([self._vr[n]])[0])
        return out

    def outputs(self) -> dict[str, Scalar]:
        return self.read(self._outputs)

    def state(self) -> dict[str, dict[str, float]]:
        """The partition's physical state: owner (asset id, `room:<id>`, `return:<pump>`) ->
        start parameter -> value. Keyed by World Model identity, so it carries over to a
        recompiled partition. Air humidity is not carried: it restarts from its default."""
        out: dict[str, dict[str, float]] = {}
        for owner, params in self.partition.state.items():
            values = self.read(params.values())
            out[owner] = {p: float(values[var]) for p, var in params.items()}
        return out

    @staticmethod
    def start_parameters(
        partition: Partition, state: Mapping[str, Mapping[str, float]]
    ) -> dict[str, float]:
        """FMU parameters that start a new instance of `partition` from `state`. Owners or
        parameters the partition does not have are ignored."""
        out: dict[str, float] = {}
        for owner, params in state.items():
            names = partition.start.get(owner, {})
            for p, value in params.items():
                if p in names:
                    out[names[p]] = value
        return out

    # --- stepping --------------------------------------------------------------------------

    def _event_iteration(self) -> None:
        more = True
        next_time, defined = math.inf, False
        while more:
            more, terminate, _, _, defined, next_time = self.fmu.newDiscreteStates()
            if terminate:
                raise RuntimeError(f"{self.partition.name} requested termination")
        self._next_event = next_time if defined else math.inf

    def _discontinuity(self, time: float, changes: Mapping[str, Scalar] | None = None) -> None:
        self.fmu.enterEventMode()
        for name, value in (changes or {}).items():
            self._write(name, value)
        self._event_iteration()
        self.fmu.enterContinuousTimeMode()
        self._solver.reset(time)

    def set_inputs(self, values: Mapping[str, Scalar]) -> None:
        """Inputs for the next step. A changed Boolean input is an event; a changed Real input
        is set in continuous time, so the integrator keeps its Jacobian (re-initialising it on
        every controller move costs one derivative evaluation per state)."""
        changed = {k: v for k, v in values.items() if self.applied.get(k) != v}
        discrete = {k: v for k, v in changed.items() if k in self._bools}
        for name, value in changed.items():
            if name not in discrete:
                self._write(name, value)
        if discrete:
            self._discontinuity(self.time, discrete)
        self.applied.update(changed)

    def advance(self, until: float) -> None:
        time = self.time
        while time < until - 1e-9:
            target = min(until, self._next_event)
            state_event, _, time = self._solver.step(time, target)
            self.solver_steps += 1
            self.fmu.setTime(time)
            step_event = False
            if self._needs_completed:
                step_event, terminate = self.fmu.completedIntegratorStep()
                if terminate:
                    raise RuntimeError(f"{self.partition.name} requested termination")
            if state_event or step_event or abs(time - self._next_event) < 1e-9:
                self.events += 1
                self._discontinuity(time)
        self.time = until

    def close(self) -> None:
        try:
            self.fmu.terminate()
            self.fmu.freeInstance()
        finally:
            shutil.rmtree(self._dir, ignore_errors=True)

"""Chiller plant controller blocks (ADR-0002 Amendment 2: controllers are runtime blocks).

The blocks are the chiller plant PLC's logic, configured only by ControlBindings. They read
measured values in the instrument's own units (°C, kPa, m³/h) through the signal bus, so a
faulty sensor misleads them as it would the real PLC, and they write commands: set points,
speeds, positions, enables. None of them computes a physical consequence.

A bad reading (None) holds a loop's output where it is, as a PLC does on a failed input.
Import this module to register the blocks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from gws_runtime.controllers import Block, SignalBus, block
from gws_runtime.values import Value, split_ref
from gws_world_model.model import ControlBinding, WorldModel

CP_WATER = 4.184
"""kJ/(kg K): with a flow in m³/h (1000 kg/m³) the load comes out in kW."""


def _number(value: Value) -> float | None:
    if isinstance(value, bool | int | float):
        return float(value)
    return None


class _PI:
    """A velocity-free PI loop with a clamped integrator (no windup)."""

    def __init__(self, kp: float, ti: float, lo: float, hi: float, start: float) -> None:
        self.kp, self.ti, self.lo, self.hi = kp, ti, lo, hi
        self.integral = start
        self.output = start

    def step(self, error: float | None, dt: float) -> float:
        if error is None:
            return self.output
        self.integral = min(max(self.integral + self.kp * error * dt / self.ti, self.lo), self.hi)
        self.output = min(max(self.integral + self.kp * error, self.lo), self.hi)
        return self.output

    def snapshot(self) -> dict[str, float]:
        return {"integral": self.integral, "output": self.output}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.integral = float(state["integral"])
        self.output = float(state["output"])


class _Loop(Block):
    """One PI loop: first read is the measurement, every drive gets the output."""

    direct = True
    """Direct acting: the output rises when the measurement is above set point."""
    setpoint_param = ""
    kp = 0.05
    ti = 60.0
    lo = 0.0
    hi = 1.0
    start = 1.0

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        super().__init__(binding, doc)
        self.pi = _PI(
            float(self.param("kp", self.kp)),
            float(self.param("ti_s", self.ti)),
            self._lo(),
            float(self.param("max_output", self.hi)),
            float(self.param("initial_output", self.start)),
        )
        self.measured: float | None = None
        self.written: float | None = None

    def _lo(self) -> float:
        return float(self.param("min_output", self.lo))

    def measure(self, bus: SignalBus) -> float | None:
        return _number(bus.read(self.binding.reads[0]))

    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        self.measured = self.measure(bus)
        self.pi.lo = self._lo()
        if str(self.param("mode", "AUTO")).upper() == "MANUAL":
            # The operator's output holds, and the integrator tracks it for a bumpless return.
            manual = float(self.param("manual_output_pct", 100 * self.pi.output)) / 100
            self.pi.integral = self.pi.output = min(max(manual, self.pi.lo), self.pi.hi)
            out = self.pi.output
        else:
            setpoint = float(self.param(self.setpoint_param, 0.0))
            error = None
            if self.measured is not None:
                error = self.measured - setpoint if self.direct else setpoint - self.measured
            out = self.pi.step(error, dt)
        if self.written is None or abs(out - self.written) > 1e-6:
            for reference in self.binding.drives:
                bus.write(reference, out)
            self.written = out

    def snapshot(self) -> dict[str, Any]:
        return {"pi": self.pi.snapshot(), "written": self.written}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.pi.restore(state["pi"])
        self.written = state.get("written")

    def signals(self) -> dict[str, float | bool]:
        out: dict[str, float | bool] = {"output": self.pi.output}
        if self.measured is not None:
            out["measured"] = self.measured
        return out


@block
class DifferentialPressure(_Loop):
    """Secondary pump speed holding the loop differential pressure.

    reads: (loop dp in kPa,). drives: pump speeds (fraction). parameters: `setpoint_kPa`,
    `min_speed_pct` (30), `kp` (per kPa, 0.001), `ti_s` (20).

    Hydraulics respond within a macro step, so the loop is a sampled one with a step of
    delay: head grows with speed squared (about 400 kPa per unit speed at full speed), and the
    gains keep the loop gain per step well below 1.
    """

    function = "dp_pid"
    direct = False
    setpoint_param = "setpoint_kPa"
    kp = 0.001
    ti = 20.0

    def _lo(self) -> float:
        return float(self.param("min_speed_pct", 30.0)) / 100


@block
class Bypass(_Loop):
    """Bypass valves relieving the loop differential pressure: they open above set point.

    reads: (loop dp in kPa,). drives: valve positions. parameters: `setpoint_kPa`.
    """

    function = "bypass_pid"
    setpoint_param = "setpoint_kPa"
    kp = 0.001
    ti = 20.0
    start = 0.0


@block
class CondenserWater(_Loop):
    """Tower fan speed holding the condenser water temperature entering the chillers.

    reads: condenser entering temperatures (°C) of the chillers, then optionally the wet
    bulb; the warmest valid chiller reading is controlled. drives: tower fan speeds.
    parameters: `setpoint_degC` (29), `min_speed_pct` (20).
    """

    function = "cw_temp"
    setpoint_param = "setpoint_degC"
    kp = 0.2
    ti = 120.0

    def _lo(self) -> float:
        return float(self.param("min_speed_pct", 20.0)) / 100

    def measure(self, bus: SignalBus) -> float | None:
        values = [
            _number(bus.read(r))
            for r in self.binding.reads
            if "TWetBul" not in r  # the wet bulb is context, not the controlled variable
        ]
        valid = [v for v in values if v is not None]
        return max(valid) if valid else None


@block
class ChilledWaterSupply(Block):
    """Chilled-water supply temperature: the chillers' leaving set point, trimmed so the
    header supply temperature (after blending in the decoupler) meets the plant set point.

    reads: (header supply temperature in °C,). drives: `<chiller>:TChwSet` (K).
    parameters: `setpoint_degC`, `trim_limit_K` (3), `ti_s` (300).
    """

    function = "chw_supply_temp"

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        super().__init__(binding, doc)
        limit = float(self.param("trim_limit_K", 3.0))
        self.pi = _PI(0.0, float(self.param("ti_s", 300.0)), -limit, limit, 0.0)
        self.pi.kp = 1.0
        self.written: float | None = None

    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        setpoint = float(self.param("setpoint_degC", 14.0))
        measured = _number(bus.read(self.binding.reads[0]))
        # Integral-only trim: the chillers already control their own leaving temperature.
        error = None if measured is None else setpoint - measured
        if error is not None:
            limit = self.pi.hi
            self.pi.integral = min(max(self.pi.integral + error * dt / self.pi.ti, -limit), limit)
            self.pi.output = self.pi.integral
        target = setpoint + self.pi.output + 273.15
        if self.written is None or abs(target - self.written) > 1e-3:
            for reference in self.binding.drives:
                bus.write(reference, target)
            self.written = target

    def snapshot(self) -> dict[str, Any]:
        return {"pi": self.pi.snapshot(), "written": self.written}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.pi.restore(state["pi"])
        self.written = state.get("written")

    def signals(self) -> dict[str, float | bool]:
        return {"trim_K": self.pi.output}


def _leg(doc: WorldModel, chiller: str, candidates: Sequence[str]) -> list[str]:
    """The driven pumps and valves on a chiller's own legs: those joined to it by connections
    that pass only through other driven pumps and valves."""
    pool = set(candidates)
    adjacent: dict[str, set[str]] = {}
    for c in doc.connections.values():
        adjacent.setdefault(c.source.node, set()).add(c.target.node)
        adjacent.setdefault(c.target.node, set()).add(c.source.node)
    found: list[str] = []
    frontier = [chiller]
    seen = {chiller}
    while frontier:
        node = frontier.pop()
        for other in sorted(adjacent.get(node, ())):
            if other in pool and other not in seen:
                seen.add(other)
                found.append(other)
                frontier.append(other)
    return sorted(found)


@block
class Staging(Block):
    """Chiller staging: how many chillers run, and which, with their pumps and valves.

    reads: (header return °C, header supply °C, header flow m³/h, then each chiller's run
    status). drives: chiller `enable`s, pump `speed`s and valve `position`s; each pump and
    valve follows the chiller whose leg it is on.

    Load = flow × cp × (return − supply). Stage up when the load exceeds `stage_up_load`
    (0.9) of the running capacity, or when a commanded chiller does not run (tripped or
    lost), for `stage_up_delay_s`; stage down when the load fits in `stage_down_load` (0.6) of
    one chiller fewer for `stage_down_delay_s`. After a change, `stage_up_inhibit_s` must pass.
    The lead chiller comes from the controller's `lead_chiller` register (the rotation block).
    """

    function = "chw_staging"

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        super().__init__(binding, doc)
        refs = [split_ref(r) for r in binding.drives]
        self.chillers = [a for a, s in refs if s == "enable"]
        others = [r for r in binding.drives if split_ref(r)[1] != "enable"]
        self.legs = {
            ch: [
                r
                for r in others
                if split_ref(r)[0] in _leg(doc, ch, [split_ref(o)[0] for o in others])
            ]
            for ch in self.chillers
        }
        self.status_refs = list(binding.reads[3:])
        self.capacity = {ch: self._capacity(ch) for ch in self.chillers}
        self.on: list[str] = []
        self.up_timer = 0.0
        self.down_timer = 0.0
        self.since_change = 1e9
        self.load_kw: float | None = None

    def _capacity(self, chiller: str) -> float:
        asset = self.doc.assets.get(chiller)
        if asset is None:
            return 0.0
        spec = self.doc.component_types[asset.type].parameters.get("q_nominal")
        value = asset.parameters.get("q_nominal", spec.default if spec else 0.0)
        return float(value) if isinstance(value, int | float) else 0.0

    def _order(self, bus: SignalBus) -> list[str]:
        lead = bus.read(f"{self.binding.controller}:lead_chiller")
        order = list(self.chillers)
        if isinstance(lead, str) and lead in order:
            i = order.index(lead)
            order = order[i:] + order[:i]
        return order

    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        t_ret = _number(bus.read(self.binding.reads[0]))
        t_sup = _number(bus.read(self.binding.reads[1]))
        flow = _number(bus.read(self.binding.reads[2]))
        self.load_kw = None
        if t_ret is not None and t_sup is not None and flow is not None:
            self.load_kw = max(flow * CP_WATER * (t_ret - t_sup) / 3.6, 0.0)
        status: dict[str, bool | None] = {}
        for ch, reference in zip(self.chillers, self.status_refs, strict=False):
            value = bus.read(reference)
            status[ch] = None if value is None else bool(value)
        # A chiller whose status cannot be read at all is not in service.
        available = [ch for ch in self._order(bus) if status.get(ch) is not None]
        failed = [ch for ch in self.on if status.get(ch) is False]
        n_min = int(self.param("min_chillers", 1))
        n_max = min(int(self.param("max_chillers", len(self.chillers))), len(available))
        if not self.on:
            self._set(available[: max(n_min, 0)], bus)
            return
        self.since_change += dt
        running_cap = sum(self.capacity[ch] for ch in self.on if ch not in failed)
        want_up = bool(failed) or (
            self.load_kw is not None
            and self.load_kw > float(self.param("stage_up_load", 0.9)) * running_cap
        )
        fewer = sum(self.capacity[ch] for ch in self.on[:-1])
        want_down = (
            not failed
            and len(self.on) > n_min
            and self.load_kw is not None
            and self.load_kw < float(self.param("stage_down_load", 0.6)) * fewer
        )
        self.up_timer = self.up_timer + dt if want_up else 0.0
        self.down_timer = self.down_timer + dt if want_down else 0.0
        if self.since_change < float(self.param("stage_up_inhibit_s", 0.0)):
            return
        if self.up_timer >= float(self.param("stage_up_delay_s", 60.0)):
            healthy = [ch for ch in self.on if ch not in failed]
            spare = [ch for ch in available if ch not in self.on]
            if spare and len(healthy) < n_max + len(failed):
                self._set([*healthy, spare[0]], bus)
            elif failed:
                self._set(healthy or self.on, bus)
        elif self.down_timer >= float(self.param("stage_down_delay_s", 300.0)):
            self._set(self.on[:-1], bus)

    def _set(self, on: list[str], bus: SignalBus) -> None:
        if on == self.on:
            return
        self.on = list(on)
        self.up_timer = self.down_timer = 0.0
        self.since_change = 0.0
        for ch in self.chillers:
            running = ch in self.on
            for reference in self.legs[ch]:
                bus.write(reference, 1.0 if running else 0.0)
            bus.write(f"{ch}:enable", running)

    def snapshot(self) -> dict[str, Any]:
        return {
            "on": list(self.on),
            "up_timer": self.up_timer,
            "down_timer": self.down_timer,
            "since_change": self.since_change,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self.on = list(state["on"])
        self.up_timer = float(state["up_timer"])
        self.down_timer = float(state["down_timer"])
        self.since_change = float(state["since_change"])

    def signals(self) -> dict[str, float | bool]:
        out: dict[str, float | bool] = {"running": float(len(self.on))}
        if self.load_kw is not None:
            out["load_kW"] = self.load_kw
        return out


@block
class Rotation(Block):
    """Lead chiller rotation by running hours: the chiller with the fewest hours leads.

    reads: each chiller's running hours. drives: (`<controller>:lead_chiller`,), the lead
    chiller's asset id. Re-evaluated every `interval_s` (3600).
    """

    function = "rotation"

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        super().__init__(binding, doc)
        self.chillers = [_asset_of(r, doc) for r in binding.reads]
        self.lead: str | None = None
        self.elapsed = 1e9

    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        self.elapsed += dt
        if self.elapsed < float(self.param("interval_s", 3600.0)):
            return
        self.elapsed = 0.0
        hours = []
        for ch, reference in zip(self.chillers, self.binding.reads, strict=True):
            value = _number(bus.read(reference))
            if value is not None:
                hours.append((value, ch))
        if not hours:
            return
        lead = min(hours)[1]
        if lead != self.lead:
            self.lead = lead
            bus.write(self.binding.drives[0], lead)

    def snapshot(self) -> dict[str, Any]:
        return {"lead": self.lead, "elapsed": self.elapsed}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.lead = state["lead"]
        self.elapsed = float(state["elapsed"])


def _asset_of(reference: str, doc: WorldModel) -> str:
    """The asset a read refers to: a point path's asset or an `<asset>:<signal>` asset."""
    binding = doc.point_bindings.get(reference)
    if binding is not None:
        source = binding.source
        asset = getattr(source, "asset", None)
        if isinstance(asset, str):
            return asset
    return split_ref(reference)[0] if ":" in reference else reference

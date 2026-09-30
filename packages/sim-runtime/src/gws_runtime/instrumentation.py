"""Instrumentation: turns the true physical state into measured readings and points (ADR-0001).

It owns sensor error, communication loss and quality, never physics. Each update it reads the
true state (SI units) and produces:

- one reading per Instrument: the true value of its `quantity`, converted to its unit, with
  its sensor faults applied, clamped to its range, and gated by the control network;
- one Sample per PointBinding, resolved from its source.

Rules:

- **AssetSignal**: a member of the asset type's point template reports the template's model
  `signal`; any other name is itself the model signal. A signal no model computes (the state
  view returns None) is BAD `not_simulated`. A network asset's signals fall back to the
  ControlNetwork's when the state view has none.
- **InstrumentSource** takes the instrument's reading, converted to the point's unit.
  **StaticValue** is a GOOD constant. **Unbound** is BAD `unbound`.
- **Aggregate** evaluates its inputs (point paths, instrument ids or `<asset>:<signal>`
  references). Each numeric input is converted to the point's unit (or, for a point without
  one, the first input's unit) where the dimensions match: that is where kW and MW, kWh and
  MWh, m3/h and L/s, L/min and L/day meet. `ratio` divides the first input by the sum of the
  rest, after converting the rest to the first input's unit when they share a dimension, and
  converts the quotient to the point's unit (`kW/RT`, `%`). Quality is the worst input's; an
  input without a value makes the aggregate valueless.
- **Range**: a reading outside its instrument's range is clamped to it and marked UNCERTAIN
  `out_of_range`, as a saturated transmitter reads. Faults apply before the clamp. Accuracy is
  recorded in the World Model but not applied as noise: an unfaulted sensor reads true.
- **Communication**: a reading or point whose asset (or any element of the instrument's
  `reports_via`) cannot reach a gateway keeps its last value and timestamp, with quality BAD
  `comm_lost`. The gateway's own monitoring signals of a network device (`Comm`, `Status`)
  stay fresh. Aggregates carry their inputs' quality.

Sensor faults (`fault(target, mode, params)`), all in the reading's unit when aimed at an
instrument id or point path:

- `bias` {bias}: adds an offset. `gain` {gain}: multiplies. `drift` {rate}: adds rate × hours
  since the fault's first update. `noise` {sigma}: adds Gaussian noise, seeded by
  (seed, target, time), so it is deterministic and independent of evaluation order.
- `freeze`: holds the value it had before the fault. `fail`: BAD `sensor_failed`, value kept.

Aimed at an asset, the mode is one of its type's fault modes of kind `sensor`. The fault mode
declares what it does through its parameters: a text parameter `signal` (default: comma-
separated model signals or point members it concerns; absent: every reading of the asset), an
optional text parameter `effect` (one of the modes above), else the first of the numeric
parameters `gain`, `bias`, `drift`/`rate`, `sigma`/`noise` it declares, else `fail`. Numeric
parameter values default to the spec's and are converted from the spec's unit to each
reading's unit as differences (a 2 K bias on a degF point reads 3.6 degF).
"""

from __future__ import annotations

import random
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.network import MONITOR_SIGNALS, ControlNetwork
from gws_runtime.units import Converter, compatible
from gws_runtime.values import Quality, Sample, StateView, Value, split_ref
from gws_world_model.model import (
    Access,
    Aggregate,
    AssetSignal,
    FaultKind,
    Instrument,
    InstrumentSource,
    PointBinding,
    PointClass,
    StaticValue,
    WorldModel,
)

MODES = ("gain", "bias", "drift", "noise", "freeze", "fail")
"""Sensor fault effects, in the order they apply."""
_AMOUNT_KEYS = {
    "gain": ("gain",),
    "bias": ("bias", "value"),
    "drift": ("rate", "drift"),
    "noise": ("sigma", "noise"),
}
_NEUTRAL = {"gain": 1.0, "bias": 0.0, "drift": 0.0, "noise": 0.0}
_RANK = {Quality.GOOD: 0, Quality.UNCERTAIN: 1, Quality.BAD: 2}

COMM_LOST = "comm_lost"
NOT_SIMULATED = "not_simulated"
SENSOR_FAILED = "sensor_failed"
OUT_OF_RANGE = "out_of_range"
UNBOUND = "unbound"


def _numeric(value: Value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _stale(last: Sample | None, t: float) -> Sample:
    if last is None:
        return Sample(None, Quality.BAD, t, COMM_LOST)
    return Sample(last.value, Quality.BAD, last.timestamp, COMM_LOST)


def _coerce(value: Value, data_type: str) -> Value:
    """A value in the Ignition data type of its point."""
    if value is None:
        return None
    try:
        if data_type == "Boolean" and not isinstance(value, str):
            return bool(value)
        if data_type.startswith("Int"):
            return int(round(float(value)))
        if data_type.startswith("Float"):
            return float(value)
    except ValueError:
        return value
    if data_type == "String" and not isinstance(value, str):
        return str(value)
    return value


def _sample_json(s: Sample) -> dict[str, Any]:
    return {
        "value": s.value,
        "quality": s.quality.value,
        "timestamp": s.timestamp,
        "reason": s.reason,
    }


def _sample_from(d: Mapping[str, Any]) -> Sample:
    return Sample(d["value"], Quality(d["quality"]), float(d["timestamp"]), d.get("reason", ""))


@dataclass
class _Fault:
    effect: str
    amount: float
    amount_unit: str | None = None
    """Unit of `amount` when it must be converted to each reading's unit (asset faults)."""
    signals: frozenset[str] | None = None
    """For an asset fault: model signals or point members it concerns; None for all."""
    start: float | None = None
    frozen: dict[str, Value] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "amount": self.amount,
            "amount_unit": self.amount_unit,
            "signals": None if self.signals is None else sorted(self.signals),
            "start": self.start,
            "frozen": dict(self.frozen),
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> _Fault:
        signals = d.get("signals")
        return cls(
            effect=d["effect"],
            amount=float(d["amount"]),
            amount_unit=d.get("amount_unit"),
            signals=None if signals is None else frozenset(signals),
            start=d.get("start"),
            frozen=dict(d.get("frozen", {})),
        )


class _NoState:
    def get(self, asset: str, signal: str) -> float | bool | str | None:
        return None

    def unit(self, asset: str, signal: str) -> str | None:
        return None


class Instrumentation:
    def __init__(
        self,
        doc: WorldModel,
        network: ControlNetwork,
        seed: int,
        scope: Collection[str] | None = None,
    ) -> None:
        """`scope` limits each update to the instruments on those assets and the points bound
        to them or to those instruments (others are evaluated only when an aggregate needs
        them); None updates the whole site."""
        self.doc = doc
        self.network = network
        self.seed = seed
        self.converter = Converter()
        self._state: StateView = _NoState()
        self._t = 0.0
        self._faults: dict[str, dict[str, _Fault]] = {}
        """Target (instrument id, point path or asset id) -> mode -> fault."""
        self._readings: dict[str, Sample] = {}
        self._points: dict[str, Sample] = {}
        self._refs: dict[str, Sample] = {}
        self._current: dict[str, Sample] = {}
        self._visiting: set[str] = set()
        self._signal_of: dict[str, tuple[str, str, str]] = {}
        """Point path -> (asset, model signal, member) for AssetSignal points."""
        for path, binding in doc.point_bindings.items():
            if isinstance(binding.source, AssetSignal):
                asset, member = binding.source.asset, binding.source.signal
                self._signal_of[path] = (asset, self._model_signal(asset, member), member)
        wanted = None if scope is None else frozenset(scope)
        self._instrument_ids = sorted(
            i for i, inst in doc.instruments.items() if wanted is None or inst.asset in wanted
        )
        self._paths = sorted(
            path
            for path, b in doc.point_bindings.items()
            if wanted is None
            or (isinstance(b.source, AssetSignal) and b.source.asset in wanted)
            or (
                isinstance(b.source, InstrumentSource)
                and doc.instruments[b.source.instrument].asset in wanted
            )
        )

    def _model_signal(self, asset: str, member: str) -> str:
        a = self.doc.assets.get(asset)
        if a is None:
            return member
        template = self.doc.component_types[a.type].point_template.get(member)
        return template.signal if template is not None and template.signal else member

    # --- faults ----------------------------------------------------------------------------

    def fault(self, target: str, mode: str, params: Mapping[str, float]) -> None:
        if target in self.doc.instruments or target in self.doc.point_bindings:
            if mode not in MODES:
                raise ValueError(f"unknown sensor fault mode {mode!r}; expected one of {MODES}")
            amount = next(
                (float(params[k]) for k in _AMOUNT_KEYS.get(mode, ()) if k in params),
                _NEUTRAL.get(mode, 0.0),
            )
            fault = _Fault(mode, amount)
        elif target in self.doc.assets:
            fault = self._asset_fault(target, mode, params)
        else:
            raise KeyError(f"{target!r} is not an instrument, point or asset")
        self._faults.setdefault(target, {})[mode] = fault

    def _asset_fault(self, asset: str, mode: str, params: Mapping[str, float]) -> _Fault:
        ctype = self.doc.component_types[self.doc.assets[asset].type]
        spec = ctype.fault_modes.get(mode)
        if spec is None or spec.kind is not FaultKind.SENSOR:
            raise ValueError(f"{ctype.id} has no sensor fault mode {mode!r}")
        declared = spec.parameters
        signal = declared.get("signal")
        signals = None
        if signal is not None and isinstance(signal.default, str) and signal.default.strip():
            signals = frozenset(s.strip() for s in signal.default.split(",") if s.strip())
        effect_spec = declared.get("effect")
        effect = str(effect_spec.default) if effect_spec is not None else ""
        if effect not in MODES:
            effect = next(
                (
                    m
                    for m in ("gain", "bias", "drift", "noise")
                    if set(_AMOUNT_KEYS[m]) & set(declared)
                ),
                "fail",
            )
        amount, unit = _NEUTRAL.get(effect, 0.0), None
        for key in _AMOUNT_KEYS.get(effect, ()):
            if key in params:
                amount = float(params[key])
            elif key in declared and isinstance(declared[key].default, int | float):
                amount = float(declared[key].default)
            else:
                continue
            unit = declared[key].unit if key in declared and effect != "gain" else None
            break
        return _Fault(effect, amount, unit, signals)

    def clear(self, target: str, mode: str | None = None) -> None:
        faults = self._faults.get(target)
        if faults is None:
            return
        if mode is None:
            faults.clear()
        else:
            faults.pop(mode, None)
        if not faults:
            del self._faults[target]

    def _faults_for(self, key: str, asset: str | None, names: Iterable[str]) -> list[_Fault]:
        found = list(self._faults.get(key, {}).values())
        if asset is not None and asset != key:
            wanted = set(names)
            found += [
                f
                for f in self._faults.get(asset, {}).values()
                if f.signals is None or f.signals & wanted
            ]
        return sorted(found, key=lambda f: MODES.index(f.effect))

    def _apply(
        self,
        t: float,
        key: str,
        faults: list[_Fault],
        value: Value,
        unit: str | None,
        last: Sample | None,
    ) -> tuple[Value, bool]:
        """The value with faults applied, and whether the sensor has failed."""
        failed = False
        for f in faults:
            if f.start is None:
                f.start = t
            if f.effect == "fail":
                failed = True
            elif f.effect == "freeze":
                if key not in f.frozen:
                    held = last.value if last is not None else None
                    f.frozen[key] = value if held is None else held
                value = f.frozen[key]
            elif _numeric(value) and isinstance(value, int | float):
                amount = f.amount
                if f.amount_unit is not None and f.effect != "gain":
                    amount = self.converter.convert(amount, f.amount_unit, unit, delta=True)
                if f.effect == "gain":
                    value = value * amount
                elif f.effect == "bias":
                    value = value + amount
                elif f.effect == "drift":
                    value = value + amount * (t - f.start) / 3600.0
                elif f.effect == "noise":
                    rng = random.Random(f"{self.seed}|{key}|{t!r}")
                    value = value + rng.gauss(0.0, amount)
        return value, failed

    # --- evaluation ------------------------------------------------------------------------

    def _true(self, asset: str, signal: str) -> float | bool | str | None:
        value = self._state.get(asset, signal)
        if value is None and self.network.is_network_asset(asset):
            value = self.network.signal(asset, signal)
        return value

    def _reaches(self, asset: str, via: Iterable[str] = ()) -> bool:
        return self.network.reachable(asset) and all(self.network.reachable(v) for v in via)

    def _measure(
        self,
        t: float,
        key: str,
        asset: str,
        names: tuple[str, ...],
        unit: str | None,
        last: Sample | None,
        via: Iterable[str] = (),
    ) -> Sample:
        """A measured value of the asset's model signal `names[0]` in `unit`."""
        signal = names[0]
        monitor = getattr(self._state, "monitored", None)
        monitored = (self.network.is_network_asset(asset) and signal in MONITOR_SIGNALS) or bool(
            monitor is not None and monitor(asset, signal)
        )
        if not monitored and not self._reaches(asset, via):
            return _stale(last, t)
        true = self._true(asset, signal)
        if true is None:
            return Sample(None, Quality.BAD, t, NOT_SIMULATED)
        value: Value = true
        if _numeric(true):
            value = self.converter.convert(float(true), self._state.unit(asset, signal), unit)
        value, failed = self._apply(t, key, self._faults_for(key, asset, names), value, unit, last)
        if failed:
            return Sample(value, Quality.BAD, t, SENSOR_FAILED)
        return Sample(value, Quality.GOOD, t)

    def _instrument(self, t: float, inst: Instrument) -> Sample:
        last = self._readings.get(inst.id)
        s = self._measure(
            t, inst.id, inst.asset, (inst.quantity,), inst.unit, last, inst.reports_via
        )
        if s.quality is not Quality.GOOD or not _numeric(s.value):
            return s
        assert isinstance(s.value, int | float)
        lo, hi = inst.range_min, inst.range_max
        if lo is not None and s.value < lo:
            return Sample(lo, Quality.UNCERTAIN, t, OUT_OF_RANGE)
        if hi is not None and s.value > hi:
            return Sample(hi, Quality.UNCERTAIN, t, OUT_OF_RANGE)
        return s

    def _point(self, t: float, path: str) -> Sample:
        done = self._current.get(path)
        if done is not None:
            return done
        if path in self._visiting:
            return Sample(None, Quality.BAD, t, "cycle")
        self._visiting.add(path)
        binding = self.doc.point_bindings[path]
        sample = self._evaluate(t, binding)
        if sample.value is not None:
            sample = Sample(
                _coerce(sample.value, binding.data_type),
                sample.quality,
                sample.timestamp,
                sample.reason,
            )
        self._visiting.discard(path)
        self._current[path] = sample
        return sample

    def _evaluate(self, t: float, binding: PointBinding) -> Sample:
        source = binding.source
        last = self._points.get(binding.path)
        if isinstance(source, AssetSignal):
            asset, signal, member = self._signal_of[binding.path]
            return self._measure(t, binding.path, asset, (signal, member), binding.unit, last)
        if isinstance(source, InstrumentSource):
            inst = self.doc.instruments[source.instrument]
            reading = self._reading(inst.id)
            value = reading.value
            if _numeric(value) and binding.unit and inst.unit:
                assert isinstance(value, int | float)
                value = self.converter.convert(value, inst.unit, binding.unit)
            if reading.quality is Quality.BAD:
                return Sample(value, reading.quality, reading.timestamp, reading.reason)
            faults = self._faults_for(binding.path, None, ())
            value, failed = self._apply(t, binding.path, faults, value, binding.unit, last)
            if failed:
                return Sample(value, Quality.BAD, t, SENSOR_FAILED)
            return Sample(value, reading.quality, reading.timestamp, reading.reason)
        if isinstance(source, StaticValue):
            return Sample(source.value, Quality.GOOD, t)
        if isinstance(source, Aggregate):
            return self._aggregate(t, binding, source)
        return Sample(None, Quality.BAD, t, UNBOUND)

    def _input(self, t: float, reference: str) -> tuple[Sample, str | None, bool]:
        """An aggregate input: its sample, its unit, and whether that unit is the model's SI
        unit (a None SI unit means the base unit of whatever it is converted to)."""
        if reference in self.doc.point_bindings:
            return self._point(t, reference), self.doc.point_bindings[reference].unit, False
        if reference in self.doc.instruments:
            return self._reading(reference), self.doc.instruments[reference].unit, False
        asset, signal = split_ref(reference)
        sample = self._measure(t, reference, asset, (signal,), None, self._refs.get(reference))
        self._refs[reference] = sample
        return sample, self._state.unit(asset, signal), True

    def _to(self, value: Value, unit: str | None, si: bool, target: str | None) -> Value:
        if not _numeric(value) or target is None or (unit is None and not si):
            return value
        assert isinstance(value, int | float)
        return self.converter.convert(value, unit, target)

    def _aggregate(self, t: float, binding: PointBinding, agg: Aggregate) -> Sample:
        inputs = [self._input(t, ref) for ref in agg.inputs]
        if not inputs:
            return Sample(None, Quality.BAD, t, UNBOUND)
        worst = max((s for s, _, _ in inputs), key=lambda s: _RANK[s.quality])
        quality, reason = worst.quality, worst.reason
        if any(s.value is None for s, _, _ in inputs):
            return Sample(None, Quality.BAD, t, reason or NOT_SIMULATED)
        result: Value
        if agg.function == "count_true":
            result = sum(1 for s, _, _ in inputs if s.value)
        elif agg.function == "ratio":
            result = self._ratio(binding, inputs)
            if result is None:
                return Sample(None, Quality.BAD, t, "undefined")
        else:
            target = binding.unit or inputs[0][1]
            values = [self._to(s.value, u, si, target) for s, u, si in inputs]
            try:
                if agg.function == "first":
                    result = values[0]
                elif agg.function == "sum":
                    result = sum(float(v) for v in values if v is not None)
                elif agg.function == "mean":
                    result = sum(float(v) for v in values if v is not None) / len(values)
                elif agg.function == "max":
                    result = max(values, key=lambda v: float(v) if v is not None else 0.0)
                else:
                    result = min(values, key=lambda v: float(v) if v is not None else 0.0)
            except (TypeError, ValueError):
                return Sample(None, Quality.BAD, t, "not_numeric")
        return Sample(result, quality, t, reason)

    def _ratio(self, binding: PointBinding, inputs: list[tuple[Sample, str | None, bool]]) -> Value:
        (first, u0, si0), rest = inputs[0], inputs[1:]
        if not rest or not all(
            _numeric(s.value) or isinstance(s.value, bool) for s, _, _ in inputs
        ):
            return None
        ud, sid = rest[0][1], rest[0][2]
        denominator = 0.0
        for s, u, si in rest:
            v = s.value
            if ud is not None and compatible(u, ud):
                v = self._to(v, u, si, ud)
            assert isinstance(v, int | float)
            denominator += float(v)
        numerator = first.value
        assert isinstance(numerator, int | float)
        if u0 is not None and ud is not None and compatible(u0, ud):
            numerator = self._to(numerator, u0, si0, ud)
            assert isinstance(numerator, int | float)
            unit: str | None = "1"
        else:
            unit = f"{u0}/{ud}" if u0 and ud and not (si0 or sid) else None
        if denominator == 0:
            return None
        quotient = float(numerator) / denominator
        if unit is not None and binding.unit and compatible(unit, binding.unit):
            return self.converter.convert(quotient, unit, binding.unit)
        return quotient

    def update(self, t: float, state: StateView) -> None:
        """Refresh every instrument reading and every point from the true state at `t`."""
        self._t = t
        self._state = state
        self._readings = {
            iid: self._instrument(t, self.doc.instruments[iid]) for iid in self._instrument_ids
        }
        self._current = {}
        self._visiting = set()
        for path in self._paths:
            self._point(t, path)
        self._points = self._current
        self._current = {}

    # --- queries ---------------------------------------------------------------------------

    def _reading(self, instrument: str) -> Sample:
        """An instrument's reading this update, measured on demand when outside the scope."""
        sample = self._readings.get(instrument)
        if sample is None:
            sample = self._instrument(self._t, self.doc.instruments[instrument])
            self._readings[instrument] = sample
        return sample

    def reading(self, instrument: str) -> Sample:
        return self._reading(instrument)

    def points(self) -> dict[str, Sample]:
        return dict(self._points)

    def read(self, reference: str) -> Value:
        """SignalBus.read: an instrument's reading or a point's value (None while BAD), or the
        true SI value of an `<asset>:<signal>` reference."""
        sample = self._readings.get(reference) or self._points.get(reference)
        if sample is not None:
            return None if sample.quality is Quality.BAD else sample.value
        if reference in self.doc.instruments or reference in self.doc.point_bindings:
            return None
        asset, signal = split_ref(reference)
        return self._true(asset, signal)

    def command_target(self, path: str) -> tuple[str, str] | None:
        binding = self.doc.point_bindings.get(path)
        if (
            binding is None
            or binding.access is not Access.READ_WRITE
            or binding.point_class is not PointClass.COMMAND
            or not isinstance(binding.source, AssetSignal)
        ):
            return None
        asset, signal, _ = self._signal_of[path]
        return asset, signal

    # --- lifecycle -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "t": self._t,
            "faults": {
                target: {mode: f.to_json() for mode, f in faults.items()}
                for target, faults in self._faults.items()
            },
            "readings": {k: _sample_json(s) for k, s in self._readings.items()},
            "points": {k: _sample_json(s) for k, s in self._points.items()},
            "refs": {k: _sample_json(s) for k, s in self._refs.items()},
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self._t = float(state.get("t", 0.0))
        self._faults = {
            target: {mode: _Fault.from_json(f) for mode, f in faults.items()}
            for target, faults in state.get("faults", {}).items()
        }
        self._readings = {k: _sample_from(v) for k, v in state.get("readings", {}).items()}
        self._points = {k: _sample_from(v) for k, v in state.get("points", {}).items()}
        self._refs = {k: _sample_from(v) for k, v in state.get("refs", {}).items()}

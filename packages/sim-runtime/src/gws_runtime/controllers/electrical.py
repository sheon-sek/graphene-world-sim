"""Cross-domain electrical controller blocks: transfer switch sequence, genset start and load
shedding (ADR-0002: data-configured runtime blocks).

Each block is configured only by its ControlBinding: what it reads, what it drives and its
timers. Import this module to register the blocks.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gws_runtime.controllers import Block, SignalBus, block
from gws_runtime.values import Value
from gws_world_model.model import ControlBinding, WorldModel

NORMAL = 1.0
EMERGENCY = 2.0


def _number(value: Value) -> float | None:
    """A reading as a number; None when it is bad or not numeric."""
    if isinstance(value, bool | int | float):
        return float(value)
    return None


@block
class TransferSequence(Block):
    """Open-transition ATS sequence.

    reads: (normal source voltage, emergency source voltage), per unit.
    drives: (switch position,): 0 open, 1 normal, 2 emergency.
    parameters: `pickup_pu` (0.9) a source is healthy at or above it; `confirm_delay` (2 s)
    normal must be unhealthy this long before a transfer; `emergency_stable` (1 s) emergency
    must be healthy this long before it takes load; `retransfer_delay` (300 s) normal must be
    healthy this long before the load goes back, unless emergency fails first;
    `initial_position` (1).

    A bad reading counts as an unhealthy source.
    """

    function = "ats"

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        super().__init__(binding, doc)
        self.position = float(self.param("initial_position", NORMAL))
        self.normal_bad = 0.0
        self.normal_good = 0.0
        self.emergency_good = 0.0

    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        pickup = float(self.param("pickup_pu", 0.9))
        v_normal = _number(bus.read(self.binding.reads[0]))
        v_emergency = _number(bus.read(self.binding.reads[1]))
        normal_ok = v_normal is not None and v_normal >= pickup
        emergency_ok = v_emergency is not None and v_emergency >= pickup
        self.normal_bad = 0.0 if normal_ok else self.normal_bad + dt
        self.normal_good = self.normal_good + dt if normal_ok else 0.0
        self.emergency_good = self.emergency_good + dt if emergency_ok else 0.0

        target = self.position
        if self.position != EMERGENCY:
            if self.normal_bad >= float(self.param("confirm_delay", 2.0)) and (
                self.emergency_good >= float(self.param("emergency_stable", 1.0))
            ):
                target = EMERGENCY
            elif normal_ok:
                target = NORMAL
        elif normal_ok and (
            not emergency_ok or self.normal_good >= float(self.param("retransfer_delay", 300.0))
        ):
            target = NORMAL
        if target != self.position:
            self.position = target
            bus.write(self.binding.drives[0], target)

    def snapshot(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "normal_bad": self.normal_bad,
            "normal_good": self.normal_good,
            "emergency_good": self.emergency_good,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self.position = float(state["position"])
        self.normal_bad = float(state["normal_bad"])
        self.normal_good = float(state["normal_good"])
        self.emergency_good = float(state["emergency_good"])

    def signals(self) -> dict[str, float | bool]:
        return {
            "position": self.position,
            "normal_bad_s": self.normal_bad,
            "normal_good_s": self.normal_good,
        }


@block
class GensetStart(Block):
    """Genset remote-start contact of a transfer switch.

    reads: (normal source voltage per unit, [transfer switch source position]).
    drives: `<genset>:start` for each genset it starts.
    parameters: `pickup_pu` (0.9); `confirm_delay` (2 s) normal unhealthy this long starts
    the gensets; `cooldown` (300 s) normal healthy, and the switch back on normal, this long
    stops them.
    """

    function = "genset_start"

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        super().__init__(binding, doc)
        self.requested = False
        self.normal_bad = 0.0
        self.restored = 0.0

    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        v_normal = _number(bus.read(self.binding.reads[0]))
        normal_ok = v_normal is not None and v_normal >= float(self.param("pickup_pu", 0.9))
        on_normal = True
        if len(self.binding.reads) > 1:
            on_normal = _number(bus.read(self.binding.reads[1])) == NORMAL
        self.normal_bad = 0.0 if normal_ok else self.normal_bad + dt
        self.restored = self.restored + dt if normal_ok and on_normal else 0.0
        if not self.requested and self.normal_bad >= float(self.param("confirm_delay", 2.0)):
            self._request(True, bus)
        elif self.requested and self.restored >= float(self.param("cooldown", 300.0)):
            self._request(False, bus)

    def _request(self, run: bool, bus: SignalBus) -> None:
        self.requested = run
        for reference in self.binding.drives:
            bus.write(reference, run)

    def snapshot(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "normal_bad": self.normal_bad,
            "restored": self.restored,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self.requested = bool(state["requested"])
        self.normal_bad = float(state["normal_bad"])
        self.restored = float(state["restored"])

    def signals(self) -> dict[str, float | bool]:
        return {"requested": self.requested}


@block
class LoadShed(Block):
    """Sheds feeders when the emergency source cannot carry the load, and restores them once
    the normal source is back.

    reads: (transfer switch source position, load it carries in W, then the available power
    of each emergency source in W).
    drives: `<feeder>:breaker` references in shedding order (first shed first).
    parameters: `shed_fraction` (0.95) of available power above which load is shed;
    `shed_delay` (2 s) between sheds; `restore_delay` (60 s) on the normal source before
    everything shed is restored, last shed first.
    """

    function = "load_shed"

    def __init__(self, binding: ControlBinding, doc: WorldModel) -> None:
        super().__init__(binding, doc)
        self.shed = 0
        self.over = 0.0
        self.normal = 0.0

    def step(self, t: float, dt: float, bus: SignalBus) -> None:
        source = _number(bus.read(self.binding.reads[0]))
        load = _number(bus.read(self.binding.reads[1])) or 0.0
        available = sum(_number(bus.read(r)) or 0.0 for r in self.binding.reads[2:])
        on_emergency = source == EMERGENCY
        limit = float(self.param("shed_fraction", 0.95)) * available
        self.over = self.over + dt if on_emergency and load > limit else 0.0
        self.normal = self.normal + dt if source == NORMAL else 0.0
        drives = self.binding.drives
        if self.over >= float(self.param("shed_delay", 2.0)) and self.shed < len(drives):
            bus.write(drives[self.shed], False)
            self.shed += 1
            self.over = 0.0
        elif self.shed and self.normal >= float(self.param("restore_delay", 60.0)):
            for reference in reversed(drives[: self.shed]):
                bus.write(reference, True)
            self.shed = 0

    def snapshot(self) -> dict[str, Any]:
        return {"shed": self.shed, "over": self.over, "normal": self.normal}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.shed = int(state["shed"])
        self.over = float(state["over"])
        self.normal = float(state["normal"])

    def signals(self) -> dict[str, float | bool]:
        return {"shed": float(self.shed)}

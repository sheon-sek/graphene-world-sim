"""Equipment models of the electrical domain.

Each class is the state and logic one ComponentType behaviour owns: a feeder breaker's trip
latch, a genset's start sequence and protection, a UPS battery, a transfer switch's position.
None of them knows about any other asset; the load flow in `network.py` couples them.

`faults` is always the active fault modes of this one asset, mode name to its parameters.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

type Faults = Mapping[str, Mapping[str, float]]

BEHAVIOUR_FEEDER = "GwsLib.Electrical.Feeder"
BEHAVIOUR_GENSET = "GwsLib.Electrical.Genset"
BEHAVIOUR_UPS = "GwsLib.Electrical.UPS"
BEHAVIOUR_TRANSFER_SWITCH = "GwsLib.Electrical.TransferSwitch"
BEHAVIOUR_UTILITY = "GwsLib.Electrical.UtilitySupply"

POSITION_OPEN = 0
POSITION_NORMAL = 1
POSITION_EMERGENCY = 2


def _flag(value: float | bool) -> bool:
    return bool(value)


class UnknownCommand(ValueError):
    """The asset's behaviour takes no such command."""


@dataclass(slots=True)
class Feeder:
    """A metered board position with its circuit breaker (meters, branch monitors).

    Commands: `breaker` (true closes, false opens), `reset` (clears a trip once its cause is
    gone). A `breaker_trip` fault trips it; it stays open until reset.
    """

    closed_cmd: bool = True
    tripped: bool = False

    @property
    def closed(self) -> bool:
        return self.closed_cmd and not self.tripped

    def advance(self, faults: Faults) -> None:
        if "breaker_trip" in faults:
            self.tripped = True

    def command(self, signal: str, value: float | bool, faults: Faults) -> None:
        if signal == "breaker":
            self.closed_cmd = _flag(value)
        elif signal == "reset":
            if _flag(value) and "breaker_trip" not in faults:
                self.tripped = False
        else:
            raise UnknownCommand(signal)

    def snapshot(self) -> dict[str, Any]:
        return {"closed_cmd": self.closed_cmd, "tripped": self.tripped}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.closed_cmd = bool(state["closed_cmd"])
        self.tripped = bool(state["tripped"])


# The engine of a genset, beyond what the load flow needs: what its controller reports.
COOLANT_STANDBY_K = 313.15
"""The jacket water heater keeps a standby engine at 40 degC, ready to take load."""
COOLANT_IDLE_K, COOLANT_FULL_K = 351.15, 363.15
"""Thermostat-controlled coolant at no load and at full load (78 and 90 degC)."""
COOLANT_LOSS_K = 391.15
"""Where the coolant of a set that lost coolant heads for (118 degC)."""
COOLANT_SHUTDOWN_K = 378.15
"""High coolant temperature shutdown (105 degC)."""
COOLANT_TAU_S = 300.0
OIL_RUNNING_PA, OIL_LOAD_PA, OIL_CRANKING_PA = 380e3, 60e3, 100e3
OIL_PREALARM_PA, OIL_SHUTDOWN_PA = 250e3, 180e3
BATTERY_FLOAT_V, BATTERY_CRANKING_V = 27.6, 23.2
CRANKING_REV_S = 3.0
"""Starter-motor speed while cranking (180 rpm)."""
POLE_PAIRS = 2
"""A four-pole alternator: 1,500 rpm at 50 Hz."""
IDLE_FRACTION = 0.05
"""A running set with less load than this fraction of prime is idling."""


@dataclass(slots=True)
class Genset:
    """A diesel genset: stopped until `start`, running after its start time, then a voltage
    source. Faults: `fail_to_start` locks it out at the end of the start attempt (over-crank),
    `shutdown` stops a running set on its short-circuit protection and locks it out,
    `emergency_stop` stops it and holds it stopped, `low_oil_pressure` lowers the oil pressure
    until the engine's own protection shuts it down, `coolant_loss` raises the coolant
    temperature until the high-temperature shutdown, and `derate` limits its available power.
    Its overload protection stops it after `overload_time` above `overload_limit` of available
    power. Commands: `start` (true runs, false stops), `reset` (clears a lockout whose cause is
    gone).

    A set whose day tank runs dry stops and locks out (`fuel`); one cranked without fuel
    over-cranks. Its engine reports coolant temperature (K, first order towards the thermostat's
    temperature for its load), oil pressure (Pa), starter battery volts, engine speed (rev/s)
    and run time (s).
    """

    p_prime_w: float
    start_time: float
    overload_limit: float
    overload_time: float
    start_cmd: bool = False
    starting_s: float = 0.0
    running: bool = False
    locked_out: bool = False
    lockout: str = ""
    """Why it is locked out: `over_crank`, `short_circuit`, `overload`, `emergency_stop`,
    `oil_pressure`, `coolant_temperature` or `fuel`."""
    overload_s: float = 0.0
    p_w: float = 0.0
    """Output at the last load-flow solution."""
    coolant_k: float = COOLANT_STANDBY_K
    run_s: float = 0.0
    fuelled: bool = True
    """Whether its day tank has fuel (the fuel system sets it)."""

    def available_w(self, faults: Faults) -> float:
        if not self.running:
            return 0.0
        derate = faults.get("derate")
        return self.p_prime_w * (derate.get("capacity_fraction", 1.0) if derate else 1.0)

    @property
    def starting(self) -> bool:
        return self.start_cmd and not self.running and not self.locked_out

    @property
    def load(self) -> float:
        return max(self.p_w, 0.0) / self.p_prime_w if self.running and self.p_prime_w else 0.0

    def oil_pressure_pa(self, faults: Faults) -> float:
        if self.running:
            pressure = OIL_RUNNING_PA + OIL_LOAD_PA * min(self.load, 1.0)
        elif self.starting:
            pressure = OIL_CRANKING_PA
        else:
            return 0.0
        leak = faults.get("low_oil_pressure")
        return pressure * (leak.get("pressure_fraction", 0.4) if leak is not None else 1.0)

    def battery_v(self) -> float:
        return BATTERY_CRANKING_V if self.starting else BATTERY_FLOAT_V

    def speed_rev_s(self, hz: float) -> float:
        if self.running:
            return hz / POLE_PAIRS
        return CRANKING_REV_S if self.starting else 0.0

    def _stop(self, lock: bool, reason: str = "") -> None:
        self.running = False
        self.starting_s = 0.0
        self.overload_s = 0.0
        if lock and not self.locked_out:
            self.lockout = reason
        self.locked_out = self.locked_out or lock

    def advance(self, dt: float, faults: Faults) -> None:
        if "emergency_stop" in faults:
            self._stop(lock=True, reason="emergency_stop")
        if self.running and "shutdown" in faults:
            self._stop(lock=True, reason="short_circuit")
        if self.running and self.oil_pressure_pa(faults) < OIL_SHUTDOWN_PA:
            self._stop(lock=True, reason="oil_pressure")
        if self.running and self.coolant_k >= COOLANT_SHUTDOWN_K:
            self._stop(lock=True, reason="coolant_temperature")
        if self.running and not self.fuelled:
            self._stop(lock=True, reason="fuel")
        if self.running:
            self.run_s += dt
            limit = self.overload_limit * self.available_w(faults)
            self.overload_s = self.overload_s + dt if self.p_w > limit else 0.0
            if self.overload_s >= self.overload_time:
                self._stop(lock=True, reason="overload")
        if not self.start_cmd:
            self._stop(lock=False)
        elif self.starting:
            self.starting_s += dt
            if self.starting_s >= self.start_time - 1e-9:
                self.starting_s = 0.0
                if "fail_to_start" in faults or not self.fuelled:
                    self.locked_out = True
                    self.lockout = "over_crank"
                else:
                    self.running = True
        if self.running:
            target = COOLANT_IDLE_K + (COOLANT_FULL_K - COOLANT_IDLE_K) * min(self.load, 1.0)
            if "coolant_loss" in faults:
                target = COOLANT_LOSS_K
        else:
            target = COOLANT_STANDBY_K
        self.coolant_k += (target - self.coolant_k) * (1 - math.exp(-dt / COOLANT_TAU_S))

    def command(self, signal: str, value: float | bool, faults: Faults) -> None:
        if signal == "start":
            self.start_cmd = _flag(value)
        elif signal == "reset":
            causes = {"fail_to_start", "shutdown", "emergency_stop", "low_oil_pressure"}
            hot = self.coolant_k >= COOLANT_SHUTDOWN_K or "coolant_loss" in faults
            if _flag(value) and not causes & faults.keys() and not hot and self.fuelled:
                self.locked_out = False
                self.lockout = ""
                self.overload_s = 0.0
        else:
            raise UnknownCommand(signal)

    def snapshot(self) -> dict[str, Any]:
        return {
            "start_cmd": self.start_cmd,
            "starting_s": self.starting_s,
            "running": self.running,
            "locked_out": self.locked_out,
            "lockout": self.lockout,
            "overload_s": self.overload_s,
            "p_w": self.p_w,
            "coolant_k": self.coolant_k,
            "run_s": self.run_s,
            "fuelled": self.fuelled,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self.start_cmd = bool(state["start_cmd"])
        self.starting_s = float(state["starting_s"])
        self.running = bool(state["running"])
        self.locked_out = bool(state["locked_out"])
        self.lockout = str(state.get("lockout", ""))
        self.overload_s = float(state["overload_s"])
        self.p_w = float(state["p_w"])
        self.coolant_k = float(state.get("coolant_k", COOLANT_STANDBY_K))
        self.run_s = float(state.get("run_s", 0.0))
        self.fuelled = bool(state.get("fuelled", True))


@dataclass(slots=True)
class Ups:
    """A double-conversion UPS.

    The inverter is a voltage source on the output bus whenever it runs, so the output never
    sees the input. The rectifier runs while the input voltage is at least `vin_min` and it
    has not failed; it then draws (output + recharge) / efficiency from the input. With the
    rectifier stopped the inverter runs from the battery until it is empty. The battery holds
    `rated power x autonomy / efficiency`, so it lasts `autonomy` at rated load.
    """

    p_rated_w: float
    efficiency: float
    capacity_j: float
    recharge_w: float
    vin_min: float
    input_power_factor: float
    energy_j: float
    rectifier_on: bool = True
    inverter_on: bool = True
    p_out_w: float = 0.0
    """Output at the last load-flow solution."""

    def capacity(self, faults: Faults) -> float:
        degraded = faults.get("battery_degraded")
        return self.capacity_j * (degraded.get("capacity_fraction", 1.0) if degraded else 1.0)

    def soc(self, faults: Faults) -> float:
        cap = self.capacity(faults)
        return min(self.energy_j / cap, 1.0) if cap > 0 else 0.0

    @property
    def on_battery(self) -> bool:
        return self.inverter_on and not self.rectifier_on

    def charge_w(self, faults: Faults) -> float:
        """Recharge power, limited to the rating's headroom above the output."""
        if not self.rectifier_on or self.energy_j >= self.capacity(faults) * (1 - 1e-9):
            return 0.0
        return max(min(self.recharge_w, self.p_rated_w - max(self.p_out_w, 0.0)), 0.0)

    def input_w(self, faults: Faults) -> float:
        if not self.rectifier_on:
            return 0.0
        out = max(self.p_out_w, 0.0) if self.inverter_on else 0.0
        return (out + self.charge_w(faults)) / self.efficiency

    def integrate(self, dt: float, faults: Faults) -> None:
        cap = self.capacity(faults)
        if self.on_battery:
            self.energy_j -= max(self.p_out_w, 0.0) / self.efficiency * dt
        else:
            self.energy_j += self.charge_w(faults) * dt
        self.energy_j = min(max(self.energy_j, 0.0), cap)

    def update_mode(self, vin_pu: float, faults: Faults) -> bool:
        """Set rectifier and inverter from the input voltage; True when either changed."""
        rectifier = vin_pu >= self.vin_min and "rectifier_failure" not in faults
        inverter = "output_fault" not in faults and (rectifier or self.energy_j > 0.0)
        changed = (rectifier, inverter) != (self.rectifier_on, self.inverter_on)
        self.rectifier_on, self.inverter_on = rectifier, inverter
        return changed

    def command(self, signal: str, value: float | bool, faults: Faults) -> None:
        raise UnknownCommand(signal)

    def snapshot(self) -> dict[str, Any]:
        return {
            "energy_j": self.energy_j,
            "rectifier_on": self.rectifier_on,
            "inverter_on": self.inverter_on,
            "p_out_w": self.p_out_w,
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        self.energy_j = float(state["energy_j"])
        self.rectifier_on = bool(state["rectifier_on"])
        self.inverter_on = bool(state["inverter_on"])
        self.p_out_w = float(state["p_out_w"])


@dataclass(slots=True)
class TransferSwitch:
    """An open-transition two-source switch. Command `position`: 0 open, 1 normal source,
    2 emergency source; the two sides are interlocked, so both can never be closed. A
    `fail_to_transfer` fault holds the current position."""

    position: int = POSITION_NORMAL

    def command(self, signal: str, value: float | bool, faults: Faults) -> None:
        if signal != "position":
            raise UnknownCommand(signal)
        position = round(float(value))
        if position not in (POSITION_OPEN, POSITION_NORMAL, POSITION_EMERGENCY):
            raise ValueError(f"transfer switch position must be 0, 1 or 2, not {value!r}")
        if "fail_to_transfer" not in faults:
            self.position = position

    def snapshot(self) -> dict[str, Any]:
        return {"position": self.position}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.position = int(state["position"])


@dataclass(slots=True)
class UtilitySupply:
    """The grid behind one site transformer. It supplies while utility power is available
    (Operating Conditions) and it has no `loss_of_supply` fault."""

    def available(self, utility: bool, faults: Faults) -> bool:
        return utility and "loss_of_supply" not in faults

    def command(self, signal: str, value: float | bool, faults: Faults) -> None:
        raise UnknownCommand(signal)

"""Behaviour bindings: how each ComponentType's `behaviour` becomes a GwsLib Modelica model.

This is the Simulation & Domain Models layer of ADR-0001 for the thermofluid domain. A
behaviour says which Modelica class models the type, how the World Model's ports and
parameters map onto it, which inputs the runtime drives, which outputs are the asset's true
state, which internal variables carry state across a rebuild, and how each of the type's own
fault modes acts on it. Nothing here names an asset.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from gws_world_model.model import Scalar

type Params = Mapping[str, Scalar]
"""An asset's parameters with the type's defaults filled in, in World Model units."""


def si(value: float, unit: str | None) -> float:
    """World Model parameter units to SI. Only the units the library uses for parameters."""
    match unit:
        case "degC":
            return value + 273.15
        case "kW" | "kVA":
            return value * 1e3
        case "kPa":
            return value * 1e3
        case "%":
            return value / 100
        case "h":
            return value * 3600
        case _:
            return value


R134A_A, R134A_B = 15.40, 2655.0
"""ln(p / kPa) = A - B / T: R-134a saturation pressure, within 2 % from 0 to 50 °C."""


def _psat_pa(t_k: float) -> float:
    return math.exp(R134A_A - R134A_B / t_k) * 1e3


def _f(s: Mapping[str, float | bool | str], name: str) -> float:
    value = s[name]
    if isinstance(value, str):
        raise ValueError(f"{name} is not a number")
    return float(value)


def _evap_approach(s: Mapping[str, float | bool | str]) -> float:
    """Chilled water leaving minus evaporating temperature: the approach grows with load."""
    return 0.5 + 2.0 * _f(s, "PLR")


def _cond_k(s: Mapping[str, float | bool | str]) -> float:
    """Condensing temperature: the approach over the leaving condenser water grows with load."""
    return _f(s, "TCwLvg") + 0.5 + 2.5 * _f(s, "PLR")


def _chiller_state(s: Mapping[str, float | bool | str]) -> str:
    if s.get("tripped"):
        return "TRIPPED"
    if s.get("running"):
        return "RUNNING"
    return "STOPPED" if s.get("enable", True) else "DISABLED"


TANK_T_CHARGED_K, TANK_T_SPENT_K = 287.15, 293.15
"""A buffer tank full of design supply water is charged; at design return water, spent."""
TANK_FLOW_MIN = 0.05
"""kg/s below which the tank is standing."""
TANK_T_ALARM_K = 291.15


def _tank_mode(s: Mapping[str, float | bool | str]) -> int:
    """1 charging (colder water in than the tank holds), 2 discharging, 0 standing."""
    if abs(_f(s, "m_flow")) < TANK_FLOW_MIN:
        return 0
    return 1 if _f(s, "TEnt") < _f(s, "T") - 0.05 else 2


def _num(params: Params, name: str) -> float:
    value = params[name]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"parameter {name} is not a number: {value!r}")
    return float(value)


def modelica_real(value: float) -> str:
    text = repr(float(value))
    return text if math.isfinite(value) else "0.0"


@dataclass(frozen=True, slots=True)
class Input:
    kind: Literal["real", "bool"]
    unit: str
    default: float | bool
    """Value when nothing else sets it."""
    parameter: str | None = None
    """World Model parameter that sets this input (a live set point or command); its value is
    converted with `si` from the parameter's unit."""
    environment: str | None = None
    """Operating-condition signal that drives this input (`TWetBulb`)."""
    supply: bool = False
    """The supply voltage the electrical domain computes for the asset (`V_pu`)."""
    liquid_heat: bool = False
    """The liquid-cooled share of the IT heat in the room the asset serves (a CDU)."""
    fire: bool = False
    """Whether the fire zone the asset's `fire` port is connected to is in alarm."""


@dataclass(frozen=True, slots=True)
class Output:
    kind: Literal["real", "bool"]
    unit: str


@dataclass(frozen=True, slots=True)
class FaultAction:
    """How one fault mode of the type acts on the model.

    - `override`: inputs forced to fixed values while active (a trip stops the drive).
    - `freeze`: inputs held at their value when the fault became active (a stuck actuator).
    - `scale`: fault parameter -> input it drives directly (a degradation that is a model
      input: the input takes the parameter's value, 1 when healthy).
    - `rebuild`: fault parameter -> (Modelica parameter, exponent): the parameter is multiplied
      by the fault parameter raised to the exponent. Applied warm: the partition is
      re-instantiated with the scaled parameter and its state carried over.
    - `offset`: see below.

    A sensor fault mode acts on instrumentation; an `offset` action adds its effect on the
    equipment's own control.
    """

    override: Mapping[str, float | bool] = field(default_factory=dict)
    freeze: tuple[str, ...] = ()
    scale: Mapping[str, str] = field(default_factory=dict)
    rebuild: Mapping[str, tuple[str, float]] = field(default_factory=dict)
    offset: Mapping[str, tuple[str, float]] = field(default_factory=dict)
    """Input -> (fault parameter, sign): the input is shifted by sign × the parameter. For a
    sensor inside the equipment that its own control loop uses: the equipment controls its
    reading to the set point, so the true value misses the set point by the error."""
    latching: bool = False
    """A latching trip stays active after it clears until the asset is reset."""


type Derived = tuple[str | None, Callable[[Mapping[str, float | bool | str]], float | bool | str]]


@dataclass(frozen=True, slots=True)
class Setting:
    """A set point the unit's own controller holds but its model does not act on (a CRAC's
    return-air set point beside the supply-air one it controls). It starts at a World Model
    parameter, reads back as a signal and an operator can write it."""

    unit: str
    parameter: str
    default: float


"""A derived signal's SI unit and how it is computed from the asset's other signals."""


@dataclass(frozen=True, slots=True)
class Behaviour:
    modelica: str
    ports: Mapping[str, str]
    """World Model port -> Modelica fluid port. Unlisted ports (power, net) are not fluid."""
    passages: tuple[tuple[str, str], ...]
    """(inlet, outlet) Modelica port pairs that one stream flows through."""
    medium: Mapping[str, Literal["water", "air"]]
    """Modelica fluid port -> medium."""
    inputs: Mapping[str, Input]
    outputs: Mapping[str, Output]
    modifiers: Callable[[Params], dict[str, str]]
    """Modelica modifiers from the asset's parameters. Every value must be a literal so the
    FMU keeps it settable."""
    state: Mapping[str, str] = field(default_factory=dict)
    """Start parameter -> internal variable, for state transfer across a rebuild."""
    power: str | None = None
    """Output carrying the electrical power the asset draws, in W."""
    pump: bool = False
    """The model moves fluid: a closed loop needs at least one."""
    faults: Mapping[str, FaultAction] = field(default_factory=dict)
    """ComponentType fault mode name -> how it acts."""
    points: Mapping[str, str] = field(default_factory=dict)
    """Ignition point member or instrument quantity -> the model signal it reports. `on:<x>`
    reports whether output x is non-zero (a run status). `tripped` and `run_hours` are kept
    by the runtime: a protection trip is latched, and running time is accumulated."""
    derived: Mapping[str, Derived] = field(default_factory=dict)
    """Signals computed from the asset's other signals each step (SI; unit, function): what
    an instrument on the equipment reads that the model does not output directly."""
    alarms: tuple[str, ...] = ()
    """Boolean outputs that are alarms: `alarm` (a unit's summary alarm) is true while the
    asset is tripped or any of them is true."""
    settings: Mapping[str, Setting] = field(default_factory=dict)
    """Signal -> a set point held by the unit's controller that the model does not use."""
    external: Mapping[str, str] = field(default_factory=dict)
    """World Model fluid ports the model does not expose as fluid ports, and why: a port whose
    medium the model makes itself (a PAHU's outdoor air), or one another domain carries (a
    tower's makeup water). Their connections are not part of the thermofluid model."""


_V = Input("real", "1", 1.0, supply=True)
MAINS_HZ = 50.0
"""A variable-speed drive's output frequency at full speed."""
_W = "W"
_K = "K"


def _outputs(**units: str) -> dict[str, Output]:
    return {
        name: Output("bool" if unit == "bool" else "real", unit) for name, unit in units.items()
    }


def _chiller(p: Params) -> dict[str, str]:
    q = si(_num(p, "q_nominal"), "kW")
    t_set = si(_num(p, "chw_supply_temp_set"), "degC")
    record = (
        "Buildings.Fluid.Chillers.Data.ElectricEIR.ElectricEIRChiller_Carrier_19XR_1076kW_5_52COP_Vanes("
        f"QEva_flow_nominal={modelica_real(-q)}, "
        f"COP_nominal={modelica_real(_num(p, 'cop_nominal'))}, "
        f"mEva_flow_nominal={modelica_real(_num(p, 'm_chw_flow_nominal'))}, "
        f"mCon_flow_nominal={modelica_real(_num(p, 'm_cw_flow_nominal'))}, "
        f"TEvaLvg_nominal={modelica_real(t_set)}, "
        f"TEvaLvgMax={modelica_real(t_set + 6)})"
    )
    return {
        "per": record,
        "dpChw_nominal": modelica_real(si(_num(p, "dp_chw_nominal"), "kPa")),
        "dpCw_nominal": modelica_real(si(_num(p, "dp_cw_nominal"), "kPa")),
        "flowSwitchFraction": modelica_real(_num(p, "flow_switch_fraction")),
        "VTrip_pu": modelica_real(_num(p, "v_trip_pu")),
    }


def _pump(p: Params) -> dict[str, str]:
    return {
        "m_flow_nominal": modelica_real(_num(p, "m_flow_nominal")),
        "dp_nominal": modelica_real(si(_num(p, "dp_nominal"), "kPa")),
        "VTrip_pu": modelica_real(_num(p, "v_trip_pu")),
    }


def _valve(p: Params) -> dict[str, str]:
    return {
        "m_flow_nominal": modelica_real(_num(p, "m_flow_nominal")),
        "dp_nominal": modelica_real(si(_num(p, "dp_nominal"), "kPa")),
    }


def _tower(p: Params) -> dict[str, str]:
    m = _num(p, "m_flow_nominal")
    twb = si(_num(p, "wet_bulb_design"), "degC")
    t_sup = si(_num(p, "cw_supply_temp_design"), "degC")
    t_range = si(_num(p, "q_nominal"), "kW") / (m * 4184)
    return {
        "m_flow_nominal": modelica_real(m),
        "dp_nominal": modelica_real(si(_num(p, "dp_nominal"), "kPa")),
        "PFan_nominal": modelica_real(si(_num(p, "fan_power_nominal"), "kW")),
        "TAirInWB_nominal": modelica_real(twb),
        "TApp_nominal": modelica_real(t_sup - twb),
        "TRan_nominal": modelica_real(t_range),
        "VTrip_pu": modelica_real(_num(p, "v_trip_pu")),
    }


def _tank(p: Params) -> dict[str, str]:
    return {
        "V": modelica_real(_num(p, "volume")),
        "m_flow_nominal": modelica_real(_num(p, "m_flow_nominal")),
    }


def _branch(p: Params) -> dict[str, str]:
    return {
        "m_flow_nominal": modelica_real(_num(p, "m_flow_nominal")),
        "dp_nominal": modelica_real(si(_num(p, "dp_nominal"), "kPa")),
    }


def _fan_coil(p: Params) -> dict[str, str]:
    return {
        "Q_flow_nominal": modelica_real(si(_num(p, "q_flow_nominal"), "kW")),
        "mWat_flow_nominal": modelica_real(_num(p, "m_wat_flow_nominal")),
        "mAir_flow_nominal": modelica_real(_num(p, "m_air_flow_nominal")),
        "dpWat_nominal": modelica_real(si(_num(p, "dp_wat_nominal"), "kPa")),
        "dpAir_nominal": modelica_real(_num(p, "dp_air_nominal")),
        "VTrip_pu": modelica_real(_num(p, "v_trip_pu")),
    }


def _air_handler(p: Params) -> dict[str, str]:
    return {
        "Q_flow_nominal": modelica_real(si(_num(p, "q_nominal"), "kW")),
        "mWat_flow_nominal": modelica_real(_num(p, "m_wat_flow_nominal")),
        "mAir_flow_nominal": modelica_real(_num(p, "m_air_flow_nominal")),
        "VTrip_pu": modelica_real(_num(p, "v_trip_pu")),
    }


def _dx_unit(p: Params) -> dict[str, str]:
    return {
        "Q_flow_nominal": modelica_real(si(_num(p, "q_nominal"), "kW")),
        "COP_nominal": modelica_real(_num(p, "cop_nominal")),
        "mAir_flow_nominal": modelica_real(_num(p, "m_air_flow_nominal")),
        "TAmbTrip": modelica_real(si(_num(p, "high_pressure_ambient_limit"), "degC")),
        "VTrip_pu": modelica_real(_num(p, "v_trip_pu")),
    }


def _cdu(p: Params) -> dict[str, str]:
    return {
        "Q_flow_nominal": modelica_real(si(_num(p, "q_nominal"), "kW")),
        "mWat_flow_nominal": modelica_real(_num(p, "m_facility_flow_nominal")),
        "nPumps": str(int(_num(p, "pump_count"))),
        "VTrip_pu": modelica_real(_num(p, "v_trip_pu")),
    }


_WATER_PASSAGE: Mapping[str, Literal["water", "air"]] = {"inlet": "water", "outlet": "water"}
_PUMP_PORTS = {
    "chw_in": "inlet",
    "chw_out": "outlet",
    "cw_in": "inlet",
    "cw_out": "outlet",
}

BEHAVIOURS: dict[str, Behaviour] = {
    "GwsLib.Chiller": Behaviour(
        modelica="GwsLib.Chiller",
        ports={"chw_in": "chw_in", "chw_out": "chw_out", "cw_in": "cw_in", "cw_out": "cw_out"},
        passages=(("chw_in", "chw_out"), ("cw_in", "cw_out")),
        medium={"chw_in": "water", "chw_out": "water", "cw_in": "water", "cw_out": "water"},
        inputs={
            "enable": Input("bool", "1", True),
            "TChwSet": Input("real", _K, 287.15, parameter="chw_supply_temp_set"),
            "V_pu": _V,
        },
        outputs=_outputs(
            P=_W,
            QEva=_W,
            PLR="1",
            TChwEnt=_K,
            TChwLvg=_K,
            TCwEnt=_K,
            TCwLvg=_K,
            mChw_flow="kg/s",
            mCw_flow="kg/s",
            running="bool",
        ),
        modifiers=_chiller,
        state={"TChw_start": "chi.vol2.T", "TCw_start": "chi.vol1.T"},
        power="P",
        faults={
            "trip": FaultAction(override={"enable": False}, latching=True),
            "capacity_loss": FaultAction(
                rebuild={"capacity_fraction": ("per.QEva_flow_nominal", 1.0)}
            ),
            "condenser_fouling": FaultAction(rebuild={"cop_fraction": ("per.COP_nominal", 1.0)}),
            # The chiller controls its leaving temperature on this sensor: reading high by b,
            # it delivers water b colder than its set point.
            "chw_temp_sensor": FaultAction(offset={"TChwSet": ("bias", -1.0)}),
        },
        points={
            "On_Off": "running",
            "Input Power": "P",
            "System Failure_Trip": "tripped",
            "General Alarm": "tripped",
            "HasAlarm": "tripped",
            "Unit Operating Hours": "run_hours",
            "Compressor Motor Current": "motor_current",
            "Evaporator - Low Pressure": "p_evap",
            "Evaporator Small Temperature Difference": "evap_approach",
            "Condenser - High Pressure": "p_cond",
            "Condenser Pressure": "p_cond",
            "Discharge - High Temperature": "T_discharge",
            "Discharge - Low Temperature": "T_liquid",
            "System Start Times": "starts_day",
            "Unit Cycling Fault Code": "short_cycling",
            "Unit Safety Fault Code": "tripped",
            "state": "state",
            "sequenceState": "sequence_state",
            "readyToStart": "ready_to_start",
            "readyToStop": "running",
        },
        derived={
            # Motor current follows the compressor's load, as a share of full-load amps.
            "motor_current": ("1", lambda s: _f(s, "PLR")),
            "evap_approach": ("K", _evap_approach),
            "p_evap": ("Pa", lambda s: _psat_pa(_f(s, "TChwLvg") - _evap_approach(s))),
            "p_cond": ("Pa", lambda s: _psat_pa(_cond_k(s))),
            # Hot gas leaves the compressor superheated, more so at high lift and load; the
            # liquid leaves the condenser a few kelvin subcooled.
            "T_discharge": ("K", lambda s: _cond_k(s) + 8.0 + 17.0 * _f(s, "PLR")),
            "T_liquid": ("K", lambda s: _cond_k(s) - 4.0),
            "state": (None, _chiller_state),
            "sequence_state": (
                None,
                lambda s: "ON LINE"
                if s.get("running")
                else "STANDBY"
                if not s.get("tripped")
                else "LOCKED OUT",
            ),
            "ready_to_start": (None, lambda s: not s.get("running") and not s.get("tripped")),
        },
    ),
    "GwsLib.Pump": Behaviour(
        modelica="GwsLib.Pump",
        ports=_PUMP_PORTS,
        passages=(("inlet", "outlet"),),
        medium=_WATER_PASSAGE,
        inputs={
            "speed": Input("real", "1", 1.0, parameter="speed"),
            "V_pu": _V,
            "headFactor": Input("real", "1", 1.0),
        },
        outputs=_outputs(P=_W, m_flow="kg/s", dp="Pa", pSuc="Pa", pDis="Pa", TEnt=_K, y="1"),
        modifiers=_pump,
        state={"T_start": "mov.heatPort.T"},
        power="P",
        pump=True,
        faults={
            "trip": FaultAction(override={"speed": 0.0}, latching=True),
            "impeller_wear": FaultAction(scale={"head_fraction": "headFactor"}),
        },
        points={
            "On_Off": "on:y",
            "Power": "P",
            "TChwSup": "TEnt",
            "TChwRet": "TRet",
            "dpLoop": "dp",
        },
        derived={"frequency": ("Hz", lambda s: MAINS_HZ * _f(s, "speed"))},
    ),
    "GwsLib.Valve": Behaviour(
        modelica="GwsLib.Valve",
        ports=_PUMP_PORTS,
        passages=(("inlet", "outlet"),),
        medium=_WATER_PASSAGE,
        inputs={"position": Input("real", "1", 1.0, parameter="position")},
        outputs=_outputs(y="1", m_flow="kg/s", dp="Pa"),
        modifiers=_valve,
        faults={
            "stuck": FaultAction(freeze=("position",)),
            "valve_fail_to_close": FaultAction(freeze=("position",)),
        },
        points={"On_Off": "on:y"},
    ),
    "GwsLib.Branch": Behaviour(
        modelica="GwsLib.Branch",
        ports={"chw_in": "inlet", "chw_out": "outlet"},
        passages=(("inlet", "outlet"),),
        medium=_WATER_PASSAGE,
        inputs={},
        outputs=_outputs(m_flow="kg/s", TEnt=_K, dp="Pa"),
        modifiers=_branch,
        faults={
            "strainer_blockage": FaultAction(
                # A blocked strainer passes a fraction f of the flow at the design pressure drop,
                # so its resistance, dp at nominal flow, grows by 1 / f².
                rebuild={"flow_fraction": ("dp_nominal", -2.0)}
            )
        },
        points={
            "TChwEnt": "TEnt",
            "TChwLvg": "TEnt",
            "valve1Position": "valves_open",
            "valve2Position": "valves_open",
        },
        derived={
            # The branch's isolating valves are not modelled: they read open while it flows.
            "valves_open": ("1", lambda s: 1.0 if abs(_f(s, "m_flow")) > TANK_FLOW_MIN else 0.0),
        },
    ),
    "GwsLib.CoolingTower": Behaviour(
        modelica="GwsLib.CoolingTower",
        ports={"cw_in": "inlet", "cw_out": "outlet"},
        passages=(("inlet", "outlet"),),
        medium=_WATER_PASSAGE,
        inputs={
            "fanSpeed": Input("real", "1", 1.0, parameter="fan_speed"),
            "V_pu": _V,
            "TWetBulb": Input("real", _K, 298.15, environment="TWetBulb"),
            "capFactor": Input("real", "1", 1.0),
        },
        outputs=_outputs(PFan=_W, TLvg=_K, TEnt=_K, m_flow="kg/s"),
        modifiers=_tower,
        state={"T_start": "tow.vol.T"},
        power="PFan",
        faults={
            "fan_trip": FaultAction(override={"fanSpeed": 0.0}, latching=True),
            "fill_fouling": FaultAction(scale={"capacity_fraction": "capFactor"}),
        },
        points={
            "On_Off": "on:PFan",
            "Power": "PFan",
            "System Failure_Trip": "tripped",
            "General Alarm": "tripped",
            "HasAlarm": "tripped",
            "runHours": "run_hours",
        },
        derived={
            # The tower's load is its fan's: the share of full speed it runs at.
            "PLR": ("1", lambda s: _f(s, "fanSpeed") if _f(s, "PFan") > 0 else 0.0),
        },
    ),
    "GwsLib.BufferTank": Behaviour(
        modelica="GwsLib.BufferTank",
        ports={"chw_in": "inlet", "chw_out": "outlet"},
        passages=(("inlet", "outlet"),),
        medium=_WATER_PASSAGE,
        inputs={},
        outputs=_outputs(T=_K, TEnt=_K, m_flow="kg/s", p="Pa"),
        modifiers=_tank,
        state={"T_start": "vol.T"},
        points={
            "Chilled Water Supply Inlet Temp": "TEnt",
            "Chilled Water Supply Outlet Temp": "T",
            # Modelled well mixed: every stratified sensor reads the tank temperature.
            **{f"Buffer Tank Stratified Temperature {n}": "T" for n in range(1, 7)},
            "p": "p",
            "level": "level",
            "chargeFraction": "charge",
            "status": "status",
            "Chiller Buffer Tank Mode Status": "mode",
            "Chiller Buffer Tank Recharge": "charging",
            "Chiller Buffer Tank Recharge Pipe Flow Meter": "recharge_flow",
            "Buffer Tank Temperature Alarm": "T_alarm",
            # The recharge line's valves are not modelled: they read as the tank's mode sets
            # them (normally-closed open and normally-open closed while recharging).
            "Recharge Valve Control": "recharge_valve",
            "Recharge Valve Feedback": "recharge_valve",
            "Normally Closed Valve Open Command": "charging",
            "Normally Closed Valve Open Status": "nc_open",
            "Normally Closed Valve Close Command": "not_charging",
            "Normally Closed Valve Close Status": "nc_closed",
            "Normally Opened Valve Open Command": "not_charging",
            "Normally Opened Valve Open Status": "nc_closed",
            "Normally Opened Valve Close Command": "charging",
            "Normally Opened Valve Close Status": "nc_open",
            "Normally Closed Valve Fail To Close": "valve_fault",
            "Normally Opened Valve Fail To Open": "valve_fault",
            "Normally Closed Valve Auto_Manual Mode": "Auto_Manual",
            "Normally Opened Valve Auto_Manual Mode": "Auto_Manual",
        },
        derived={
            # A closed, pressurised tank is always full.
            "level": ("1", lambda s: 1.0),
            "charge": (
                "1",
                lambda s: min(
                    max((TANK_T_SPENT_K - _f(s, "T")) / (TANK_T_SPENT_K - TANK_T_CHARGED_K), 0.0),
                    1.0,
                ),
            ),
            "mode": (None, _tank_mode),
            "charging": (None, lambda s: _tank_mode(s) == 1),
            "not_charging": (None, lambda s: _tank_mode(s) != 1),
            "nc_open": (None, lambda s: int(_tank_mode(s) == 1)),
            "nc_closed": (None, lambda s: int(_tank_mode(s) != 1)),
            "recharge_valve": ("1", lambda s: 1.0 if _tank_mode(s) == 1 else 0.0),
            "recharge_flow": (
                "m3/s",
                lambda s: abs(_f(s, "m_flow")) / 1000 if _tank_mode(s) == 1 else 0.0,
            ),
            "status": (None, lambda s: ("STANDBY", "CHARGING", "DISCHARGING")[_tank_mode(s)]),
            "T_alarm": (None, lambda s: _f(s, "T") > TANK_T_ALARM_K),
            "valve_fault": (None, lambda s: bool(s.get("tripped"))),
        },
        alarms=("T_alarm",),
    ),
    "GwsLib.FanCoil": Behaviour(
        modelica="GwsLib.FanCoil",
        ports={"chw_in": "chw_in", "chw_out": "chw_out", "air_in": "air_in", "air_out": "air_out"},
        passages=(("chw_in", "chw_out"), ("air_in", "air_out")),
        medium={"chw_in": "water", "chw_out": "water", "air_in": "air", "air_out": "air"},
        inputs={
            "fanSpeed": Input("real", "1", 1.0),
            "V_pu": _V,
            "airFactor": Input("real", "1", 1.0),
            "TSupSet": Input("real", _K, 291.15, parameter="supply_air_temp_set"),
        },
        outputs=_outputs(
            Q=_W,
            PFan=_W,
            TSupAir=_K,
            TRetAir=_K,
            TChwEnt=_K,
            TChwLvg=_K,
            mChw_flow="kg/s",
            mAir_flow="kg/s",
            yVal="1",
            yFan="1",
            dpFan="Pa",
            phiSup="1",
            phiRet="1",
            filterAlarm="bool",
        ),
        modifiers=_fan_coil,
        alarms=("filterAlarm",),
        state={"TRet_start": "fan.mov.heatPort.T", "xiVal_start": "val.con.I.y"},
        power="PFan",
        faults={
            "trip": FaultAction(override={"fanSpeed": 0.0}, latching=True),
            "filter_choke": FaultAction(scale={"airflow_fraction": "airFactor"}),
            # The unit's valve controls on this sensor: reading high by b, it delivers air b
            # colder than its set point.
            "supply_air_sensor": FaultAction(offset={"TSupSet": ("bias", -1.0)}),
        },
        points={
            "On_Off": "on:PFan",
            "EC Fan Run Status": "on:PFan",
            "Operation": "on:PFan",
            "Chilled Water Supply Temperature": "TChwEnt",
            "Chilled Water Return Temperature": "TChwLvg",
            "Flowrate": "mChw_flow",
            "Supply Air Temperature": "TSupAir",
            "Return Air Temperature": "TRetAir",
            "TRetAir": "TRetAir",
            "Supply Air Relative Humidity": "phiSup",
            "Return Air Relative Humidity": "phiRet",
            "Unit Static Pressure": "dpFan",
            "Filter Choke Alarm": "filterAlarm",
            "Air Differential Pressure Alarm": "filterAlarm",
            "Energy Monitoring System": "energy:PFan",
            "Fault": "tripped",
            "System Failure_Trip": "tripped",
            "Common Fault": "tripped",
            "Common Alarm": "alarm",
            "HasAlarm": "alarm",
            "Loss of Signal Alarm": "comm_lost",
            "Master Loss Communication Alarm": "comm_lost",
            "Unit Loss Communication Alarm": "comm_lost",
        },
    ),
    "GwsLib.AirHandler": Behaviour(
        modelica="GwsLib.AirHandler",
        ports={"chw_in": "chw_in", "chw_out": "chw_out", "air_out": "air_out"},
        passages=(("chw_in", "chw_out"),),
        medium={"chw_in": "water", "chw_out": "water", "air_out": "air"},
        inputs={
            "fanSpeed": Input("real", "1", 1.0),
            "V_pu": _V,
            "airFactor": Input("real", "1", 1.0),
            "TSupSet": Input("real", _K, 289.15, parameter="supply_air_temp_set"),
            "TOut": Input("real", _K, 303.15, environment="TDryBulb"),
            "XOut": Input("real", "1", 0.019, environment="XOut"),
            "fireStop": Input("bool", "1", False, fire=True),
        },
        outputs=_outputs(
            Q=_W,
            PFan=_W,
            TSupAir=_K,
            TOutAir=_K,
            TChwEnt=_K,
            TChwLvg=_K,
            mChw_flow="kg/s",
            mAir_flow="kg/s",
            yVal="1",
            yFan="1",
            phiSup="1",
            phiOut="1",
            filterAlarm="bool",
            fireAlarm="bool",
        ),
        modifiers=_air_handler,
        alarms=("filterAlarm", "fireAlarm"),
        state={"TFan_start": "fan.mov.heatPort.T", "xiVal_start": "val.con.I.y"},
        power="PFan",
        faults={
            "trip": FaultAction(override={"fanSpeed": 0.0}, latching=True),
            "filter_choke": FaultAction(scale={"airflow_fraction": "airFactor"}),
            "supply_air_sensor": FaultAction(offset={"TSupSet": ("bias", -1.0)}),
        },
        settings={
            "TRetSet": Setting(_K, "return_air_temp_set", 297.15),
            "phiRetSet": Setting("1", "return_air_rh_set", 0.5),
            "phiSupSet": Setting("1", "supply_air_rh_set", 0.55),
        },
        points={
            "On_Off": "on:PFan",
            "Fan On_Off": "on:PFan",
            "Supply Air Temperature": "TSupAir",
            "Supply Air Relative Humidity": "phiSup",
            "Supply Air Temperature Setpoint": "TSupSet",
            "Supply Air Relative Humidity Setpoint": "phiSupSet",
            "Return Air Temperature": "TRetAir",
            "Return Air Relative Humidity": "phiRet",
            "Return Air Temperature Setpoint": "TRetSet",
            "Return Air Relative Humidity Setpoint": "phiRetSet",
            "Filter Sensor Alarm": "filterAlarm",
            "Main Fire Alarm": "fireAlarm",
            "System Failure_Trip": "tripped",
            "HasAlarm": "alarm",
        },
        external={
            "air_in": "outdoor air: the model draws it at the outdoor condition",
        },
    ),
    "GwsLib.DXUnit": Behaviour(
        modelica="GwsLib.DXUnit",
        ports={"air_in": "air_in", "air_out": "air_out"},
        passages=(("air_in", "air_out"),),
        medium={"air_in": "air", "air_out": "air"},
        inputs={
            "fanSpeed": Input("real", "1", 1.0),
            "V_pu": _V,
            "airFactor": Input("real", "1", 1.0),
            "capFactor": Input("real", "1", 1.0),
            "TSupSet": Input("real", _K, 291.15, parameter="supply_air_temp_set"),
            "TOut": Input("real", _K, 303.15, environment="TDryBulb"),
        },
        outputs=_outputs(
            Q=_W,
            QSen=_W,
            P=_W,
            PFan=_W,
            TSupAir=_K,
            TRetAir=_K,
            mAir_flow="kg/s",
            speRat="1",
            compressor1="bool",
            compressor2="bool",
            highPressure="bool",
            yFan="1",
            phiSup="1",
            phiRet="1",
            filterAlarm="bool",
        ),
        modifiers=_dx_unit,
        alarms=("filterAlarm", "highPressure"),
        derived={
            # Two compressors: the first carries the speed ratio up to half, the second the rest.
            "stage1": ("1", lambda s: min(float(s["speRat"]) / 0.5, 1.0)),
            "stage2": ("1", lambda s: max(float(s["speRat"]) - 0.5, 0.0) / 0.5),
        },
        state={"TRet_start": "fan.mov.heatPort.T", "xiCom_start": "con.I.y"},
        power="P",
        faults={
            "trip": FaultAction(override={"fanSpeed": 0.0}, latching=True),
            "filter_choke": FaultAction(scale={"airflow_fraction": "airFactor"}),
            "compressor_failure": FaultAction(scale={"capacity_fraction": "capFactor"}),
            "supply_air_sensor": FaultAction(offset={"TSupSet": ("bias", -1.0)}),
        },
        settings={"TRetSet": Setting(_K, "return_air_temp_set", 297.15)},
        points={
            "On_Off": "on:PFan",
            "Fan Speed": "yFan",
            "Supply Air Temperature Setpoint": "TSupSet",
            "Return Air Temperature Setpoint": "TRetSet",
            "EC Fan Speed": "yFan",
            "Compressor On_Off Status": "compressor1",
            "Compressor Capacity": "stage1",
            "Compressor 2 Capacity": "stage2",
            "Supply Air Temperature": "TSupAir",
            "Return Air Temperature": "TRetAir",
            "Supply Air Relative Humidity": "phiSup",
            "Return Air Relative Humidity": "phiRet",
            "Filter Choke Alarm": "filterAlarm",
            "High Pressure Alarm": "highPressure",
            "Loss of Signal Alarm": "comm_lost",
            "System Failure_Trip": "tripped",
            "HasAlarm": "alarm",
        },
    ),
    "GwsLib.CDU": Behaviour(
        modelica="GwsLib.CDU",
        ports={"chw_in": "chw_in", "chw_out": "chw_out"},
        passages=(("chw_in", "chw_out"),),
        medium={"chw_in": "water", "chw_out": "water"},
        inputs={
            "QIt": Input("real", _W, 0.0, liquid_heat=True),
            "pumps": Input("real", "1", 1.0),
            "pumpFactor": Input("real", "1", 1.0),
            "V_pu": _V,
            "TSecSet": Input("real", _K, 298.15, parameter="secondary_supply_temp_set"),
        },
        outputs=_outputs(
            Q=_W,
            P=_W,
            PPump=_W,
            TSec=_K,
            TChwEnt=_K,
            TChwLvg=_K,
            mChw_flow="kg/s",
            yVal="1",
            running="bool",
        ),
        modifiers=_cdu,
        state={"TSec_start": "coolant.T", "xiVal_start": "val.con.I.y"},
        power="P",
        faults={
            "trip": FaultAction(override={"pumps": 0.0}, latching=True),
            "pump_failure": FaultAction(scale={"capacity_fraction": "pumpFactor"}),
        },
        points={
            "Unit Running Status": "running",
            "IT Load": "energy:QIt",
            "Pump 1": "energy:PPump",
            "Pump 2": "energy:PPump",
            "Pump 3": "energy:PPump",
            "HasAlarm": "alarm",
            "Facility Load": "energy:P",
            "Load Demand": "energy:QIt",
            "PUE": "pue",
        },
        derived={
            # The CDU's own power usage effectiveness: its IT heat plus its pumps over the IT.
            "pue": (
                "1",
                lambda s: (_f(s, "QIt") + _f(s, "P")) / _f(s, "QIt") if _f(s, "QIt") > 0 else 1.0,
            ),
        },
        external={
            "air_out": "no air: the CDU takes the liquid-cooled share of its room's IT heat",
        },
    ),
}

HALL = Behaviour(
    modelica="GwsLib.Hall",
    ports={},
    passages=(),
    medium={},
    inputs={"QIt": Input("real", _W, 0.0)},
    outputs=_outputs(TAir=_K, TMass=_K, phi="1", X="1"),
    modifiers=lambda _: {},
    state={"T_start": "vol.T", "TMass_start": "mass.T"},
)
"""A room's air volume. Rooms are not assets, so the compiler generates one per served room."""

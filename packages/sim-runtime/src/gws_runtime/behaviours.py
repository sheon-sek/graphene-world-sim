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


_V = Input("real", "1", 1.0, supply=True)
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


_WATER_PASSAGE: Mapping[str, Literal["water", "air"]] = {"inlet": "water", "outlet": "water"}
_PUMP_PORTS = {
    "chw_in": "inlet",
    "chw_out": "outlet",
    "cw_in": "inlet",
    "cw_out": "outlet",
    "water_in": "inlet",
    "water_out": "outlet",
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
        points={"TChwEnt": "TEnt", "TChwLvg": "TEnt"},
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
        },
    ),
    "GwsLib.BufferTank": Behaviour(
        modelica="GwsLib.BufferTank",
        ports={"chw_in": "inlet", "chw_out": "outlet"},
        passages=(("inlet", "outlet"),),
        medium=_WATER_PASSAGE,
        inputs={},
        outputs=_outputs(T=_K, TEnt=_K, m_flow="kg/s"),
        modifiers=_tank,
        state={"T_start": "vol.T"},
        points={
            "Chilled Water Supply Inlet Temp": "TEnt",
            "Chilled Water Supply Outlet Temp": "T",
            # Modelled well mixed: every stratified sensor reads the tank temperature.
            **{f"Buffer Tank Stratified Temperature {n}": "T" for n in range(1, 7)},
        },
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
        ),
        modifiers=_fan_coil,
        state={"TRet_start": "fan.mov.heatPort.T"},
        power="PFan",
        faults={
            "trip": FaultAction(override={"fanSpeed": 0.0}, latching=True),
            "filter_choke": FaultAction(scale={"airflow_fraction": "airFactor"}),
        },
        points={
            "On_Off": "on:PFan",
            "EC Fan Run Status": "on:PFan",
            "Chilled Water Supply Temperature": "TChwEnt",
            "Chilled Water Return Temperature": "TChwLvg",
            "Flowrate": "mChw_flow",
            "Supply Air Temperature": "TSupAir",
            "Return Air Temperature": "TRetAir",
            "Fault": "tripped",
            "Common Fault": "tripped",
            "HasAlarm": "tripped",
        },
    ),
}

HALL = Behaviour(
    modelica="GwsLib.Hall",
    ports={},
    passages=(),
    medium={},
    inputs={"QIt": Input("real", _W, 0.0)},
    outputs=_outputs(TAir=_K, TMass=_K),
    modifiers=lambda _: {},
    state={"T_start": "vol.T", "TMass_start": "mass.T"},
)
"""A room's air volume. Rooms are not assets, so the compiler generates one per served room."""

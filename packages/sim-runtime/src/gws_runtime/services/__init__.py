"""Site services: the building systems outside the thermofluid and electrical models (ADR-0002
Amendment 4). Cold water, leak detection, fire detection and protection, lifts, diesel fuel,
and the sensors that read rooms and the weather.

Each is a small Python model stepped once per macro step after the thermofluid models, reading
the state they published and the operating conditions. Their pumps, lifts and fire pumps are
electrical loads; their fire zones shut down the air handlers connected to them; their fuel
keeps the gensets running.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from gws_runtime.conditions import Conditions
from gws_runtime.services.common import Env, Signals
from gws_runtime.services.fire import FIRE_PUMP, FireSystem, RoomFire
from gws_runtime.services.fuel import FuelSystem
from gws_runtime.services.lifts import Lifts
from gws_runtime.services.sensors import WEATHER, Leak, LeakDetection, RoomSensors, weather
from gws_runtime.services.water import WaterSystem
from gws_world_model.model import WorldModel

__all__ = ["Env", "SiteServices"]

UNITS: dict[str, str] = {
    "level": "1",
    "m_flow": "kg/s",
    "speed": "1",
    "f_out": "Hz",
    "p_dis": "Pa",
    "p_set": "Pa",
    "run_s": "s",
    "inflow": "kg/s",
    "outflow": "kg/s",
    "position": "1",
    "leak_position": "m",
    "cable_length": "m",
    "T": "K",
    "phi": "1",
    "TDryBul": "K",
    "TWetBul": "K",
    "TDewPoi": "K",
    "relHum": "1",
    "pAtm": "Pa",
    "precipitation": "m",
    "winDir": "rad",
    "winSpe": "m/s",
    "fuel_flow": "kg/s",
    "fuel_total": "m3",
    "day_tank": "1",
}
"""SI units of the services' signals."""

_PUMP_POINTS = {
    "On_Off": "running",
    "HasAlarm": "alarm",
    "System Failure_Trip": "alarm",
    "Incoming Power Status": "energised",
    "Output Frequency": "f_out",
    "Actual Discharge Pressure": "p_dis",
    "Discharge Pressure Setpoint": "p_set",
    "VSD Speed Control": "speed",
    "Unit Operating Hours": "run_s",
    "Current": "I",
    "Voltage": "V_ll",
}
_TYPE_POINTS: dict[str, dict[str, str]] = {
    "valve": {"On_Off": "open"},
    "tank": {
        "Water Level": "level",
        "High_Low Water Level Alarm": "level_alarm",
        "HasAlarm": "alarm",
    },
    "Water Leak Cable Sensor": {
        "Leak Position": "leak_position",
        "Status": "status",
        "Cable Length": "cable_length",
    },
    "Environment Monitoring": {"Temperature": "T", "Humidity": "phi"},
    "Temperature and Humidity": {"Temp": "T", "Humidity": "phi"},
    "Diesel": {
        "Flow Totalizer - Flowmeter A": "fuel_total",
        "Flowmeter - Flowmeter A": "fuel_flow",
        "General Alarm - PLC Panel A": "alarm",
        "General Alarm - PLC Panel B": "low_level",
        "HasAlarm": "alarm",
        "On_Off - PLC Panel A": "panel_on",
        "On_Off - PLC Panel B": "panel_on",
        "Open_Close Feedback - Inlet Valve": "inlet_open",
        "Run_Stop - Fuel Pump A": "pump_running",
        "System Failure_Trip - Fuel Pump A": "pump_tripped",
        "System Failure_Trip - PLC Panel A": "panel_fault",
        "System Failure_Trip - PLC Panel B": "panel_fault",
        "Time-out Alarm - Inlet Valve": "inlet_timeout",
    },
}


@dataclass
class SiteServices:
    doc: WorldModel
    water: WaterSystem
    fire: FireSystem
    lifts: Lifts
    fuel: FuelSystem
    leaks: LeakDetection
    rooms: RoomSensors
    weather_stations: list[str]

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> SiteServices:
        scope = [a for a in sorted(scope) if a in doc.assets]
        return cls(
            doc=doc,
            water=WaterSystem.from_world(doc, scope),
            fire=FireSystem.from_world(doc, scope),
            lifts=Lifts.from_world(doc, scope),
            fuel=FuelSystem.from_world(doc, scope),
            leaks=LeakDetection.from_world(doc, scope),
            rooms=RoomSensors.from_world(doc, scope),
            weather_stations=[a for a in scope if doc.assets[a].type == WEATHER],
        )

    @property
    def assets(self) -> set[str]:
        return (
            self.water.assets
            | self.fire.assets
            | self.lifts.assets
            | self.fuel.assets
            | self.leaks.assets
            | set(self.rooms.room)
            | set(self.weather_stations)
        )

    @property
    def loads(self) -> set[str]:
        """Assets whose electrical demand the services set."""
        return set(self.water.pumps) | self.fire.loads | self.lifts.assets | self.fuel.assets

    def handles(self, asset: str, mode: str) -> bool:
        """Whether a fault mode on this asset acts in the services (rather than as a sensor
        fault on its readings, which the instrumentation applies)."""
        if asset in self.water.assets:
            return mode not in ("level_sensor", "pressure_sensor")
        if asset in self.fire.assets:
            return True
        if asset in self.lifts.assets:
            return True
        if asset in self.fuel.assets:
            return mode != "flowmeter"
        if asset in self.leaks.assets:
            return True
        return False

    # --- faults and commands -----------------------------------------------------------------

    def _owner(self, asset: str) -> Any:
        for system in (self.water, self.fire, self.lifts, self.fuel):
            if asset in system.assets:
                return system
        return None

    def fault(self, asset: str, mode: str, values: Mapping[str, float]) -> None:
        if asset in self.leaks.assets:
            self.leaks.faults.setdefault(asset, set()).add(mode)
            return
        owner = self._owner(asset)
        if owner is not None:
            owner.fault(asset, mode, values)

    def clear(self, asset: str, mode: str) -> None:
        if asset in self.leaks.assets:
            self.leaks.faults.get(asset, set()).discard(mode)
            return
        owner = self._owner(asset)
        if owner is not None:
            owner.clear(asset, mode)

    def command(self, asset: str, signal: str, value: float | bool) -> bool:
        owner = self._owner(asset)
        if owner is None or owner is self.lifts:
            return False
        return bool(owner.command(asset, signal, value))

    # --- stepping ----------------------------------------------------------------------------

    def step(self, env: Env, conditions: Conditions) -> None:
        self.water.step(env, conditions.water_mains_available)
        fires = {r: RoomFire(**f) for r, f in conditions.fires.items()}
        self.fire.step(env, fires)
        recalled = {lift: self.fire.reaches(lift) for lift in self.lifts.assets}
        self.lifts.step(env, recalled, conditions.lift_trips_per_hour)
        self.fuel.step(env)
        sources: dict[str, dict[str, Leak]] = {}
        for room, spec in conditions.leaks.items():
            sources.setdefault(room, {})["condition"] = Leak(
                spec.get("flow_kg_s", 0.0), spec.get("x"), spec.get("y")
            )
        for tank in self.water.tanks.values():
            leak = self.water.faults.get(tank.asset, {}).get("leak")
            loc = self.doc.assets[tank.asset].location
            if leak is not None and loc.room:
                flow = leak.get("leak_flow", 0.0) * tank.level
                sources.setdefault(loc.room, {})[tank.asset] = Leak(flow, loc.x, loc.y)
        self.leaks.step(env, sources)

    def demand_w(self, asset: str) -> float:
        if asset in self.water.pumps:
            return self.water.demand_w(asset)
        if asset in self.fire.loads:
            rated = self.doc.component_types[FIRE_PUMP].parameters["rated_power"].default
            value = self.doc.assets[asset].parameters.get("rated_power", rated)
            return self.fire.demand_w(
                asset, float(value) * 1e3 if isinstance(value, int | float) else 0.0
            )
        if asset in self.lifts.assets:
            return self.lifts.demand_w(asset)
        if asset in self.fuel.assets:
            return self.fuel.demand_w(asset)
        return 0.0

    def fire_shutdown(self, asset: str) -> bool:
        return self.fire.reaches(asset)

    def fuel_ok(self, genset: str) -> bool:
        return self.fuel.fuel_ok(genset)

    def signals(self, env: Env, conditions: Conditions) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for part in (
            self.water.signals(env),
            self.fire.signals(),
            self.lifts.signals(env),
            self.fuel.signals(env),
            self.leaks.signals(env),
            self.rooms.signals(env),
        ):
            for asset, s in part.items():
                out.setdefault(asset, {}).update(s)
        for station in self.weather_stations:
            out[station] = weather(conditions)
        return out

    def points(self, asset: str) -> dict[str, str]:
        """Point member -> signal, for an asset the services own."""
        ctype = self.doc.component_types[self.doc.assets[asset].type]
        if asset in self.water.pumps:
            aliases = dict(_PUMP_POINTS)
            power = ctype.point_template.get("Power")
            aliases["Power"] = "P" if power is not None and power.unit else "energised"
            return aliases
        if asset in self.water.valves:
            return dict(_TYPE_POINTS["valve"])
        if asset in self.water.tanks:
            return dict(_TYPE_POINTS["tank"])
        return dict(_TYPE_POINTS.get(ctype.id, {}))

    # --- lifecycle ---------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "water": self.water.snapshot(),
            "fire": self.fire.snapshot(),
            "lifts": self.lifts.snapshot(),
            "fuel": self.fuel.snapshot(),
            "leaks": self.leaks.snapshot(),
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        self.water.restore(s["water"])
        self.fire.restore(s["fire"])
        self.lifts.restore(s["lifts"])
        self.fuel.restore(s["fuel"])
        self.leaks.restore(s["leaks"])

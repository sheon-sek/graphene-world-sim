"""The chiller plant controller's registers: what its HMI shows and lets an operator set.

The plant PLC's blocks (plant.py) hold the logic; this is the PLC's register table around
them. Registers come in three kinds:

- **Settings** (set points, chiller limits, staging delays, PID modes and manual outputs)
  start at the values the ControlBindings configure and can be written by an operator. A
  written setting overrides the block parameter it maps to from the next step, as the PLC's
  own HMI would.
- **Configuration** the PLC holds but no block acts on (the rotation timetable, the cooling
  block's modes and limits, the minimum flow): registers with their commissioned values.
- **Status**, computed every step from the blocks and the plant: PID outputs, plant load,
  staging state, the lead chiller, rotations, and the alarms of the equipment it controls.

Register values are in the units the HMI shows (kPa, °C, %, s), as the block parameters are.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.controllers import Block
from gws_runtime.values import Value, split_ref
from gws_world_model.model import WorldModel

DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

SETTINGS: dict[str, tuple[tuple[str, str], ...]] = {
    "chw_dp_set": (("dp_pid", "setpoint_kPa"),),
    "pump_min_speed": (("dp_pid", "min_speed_pct"),),
    "dp_pid_mode": (("dp_pid", "mode"),),
    "dp_pid_manual_output": (("dp_pid", "manual_output_pct"),),
    "bypass_dp_set": (("bypass_pid", "setpoint_kPa"),),
    "bypass_pid_mode": (("bypass_pid", "mode"),),
    "bypass_pid_manual_output": (("bypass_pid", "manual_output_pct"),),
    "chw_supply_temp_set": (("chw_supply_temp", "setpoint_degC"),),
    "cw_supply_temp_set": (("cw_temp", "setpoint_degC"),),
    "min_chillers": (("chw_staging", "min_chillers"),),
    "max_chillers": (("chw_staging", "max_chillers"),),
    "stage_up_delay": (("chw_staging", "stage_up_delay_s"),),
    "stage_down_delay": (("chw_staging", "stage_down_delay_s"),),
    "stage_up_inhibit_delay": (("chw_staging", "stage_up_inhibit_s"),),
}
"""Writable register -> the block parameters it sets."""

FALLBACK: dict[str, Value] = {
    "pump_min_speed": 30.0,
    "dp_pid_mode": "AUTO",
    "dp_pid_manual_output": 0.0,
    "bypass_pid_mode": "AUTO",
    "bypass_pid_manual_output": 0.0,
    "cw_supply_temp_set": 29.0,
    "min_chillers": 1,
    "stage_up_delay": 60,
    "stage_down_delay": 300,
    "stage_up_inhibit_delay": 0,
}
"""A setting's value where no block parameter gives one (the blocks' own defaults)."""

CONFIGURATION: dict[str, Value] = {
    "block_chw_dp_set": 0.85,
    "chiller_load_limit": 100.0,
    "block_enabled": True,
    "loop_enable": "ENABLED",
    "block_mode": "AUTO",
    "tower_approach_set": 4.0,
    "buffer_tank_mode": "AUTO",
    "flow_mode": "AUTO",
    "min_dp_set": 40.0,
    "min_flow_set": 30.0,
    "last_command": "",
    **{f"rotation_{d}_enabled": True for d in DAYS},
    **{f"rotation_{d}_time": "06:00" for d in DAYS},
}
"""Registers the PLC holds that no block acts on, at their commissioned values."""


def _name(doc: WorldModel, asset: str) -> str:
    return doc.assets[asset].name if asset in doc.assets else asset


@dataclass
class PlantHmi:
    controller: str
    doc: WorldModel
    blocks: dict[str, Block]
    """Function -> this controller's block."""
    equipment: list[str]
    """The assets the controller drives, whose alarms it annunciates."""
    rotations: int = 0
    last_rotation: str = ""
    lead: str | None = None
    alarms: list[str] = field(default_factory=list)
    """Equipment in alarm, oldest first."""

    @classmethod
    def build(cls, controller: str, doc: WorldModel, blocks: Iterable[Block]) -> PlantHmi:
        mine = {b.function: b for b in blocks if b.binding.controller == controller}
        equipment = sorted(
            {
                split_ref(r)[0]
                for b in mine.values()
                for r in b.binding.drives
                if ":" in r and split_ref(r)[0] in doc.assets and split_ref(r)[0] != controller
            }
        )
        return cls(controller, doc, mine, equipment)

    def defaults(self) -> dict[str, Value]:
        """Every register's starting value, `<controller>:<signal>` -> value."""
        out: dict[str, Value] = {}
        for signal, targets in SETTINGS.items():
            value = FALLBACK.get(signal)
            for function, param in targets:
                blk = self.blocks.get(function)
                if blk is not None and param in blk.binding.parameters:
                    value = blk.binding.parameters[param]
            if value is not None:
                out[signal] = value
        out |= CONFIGURATION
        rotation = self.blocks.get("rotation")
        out["staging_strategy"] = str(
            rotation.param("strategy", "BY RUNNING HOURS") if rotation else "MANUAL"
        )
        return {f"{self.controller}:{k}": v for k, v in out.items()}

    def writable(self, signal: str) -> bool:
        return signal in SETTINGS or signal in CONFIGURATION

    def apply(self, register: Mapping[str, Value]) -> None:
        """Hand the settings to the blocks, as overrides of their parameters."""
        for signal, targets in SETTINGS.items():
            value = register.get(f"{self.controller}:{signal}")
            if value is None:
                continue
            for function, param in targets:
                blk = self.blocks.get(function)
                if blk is not None:
                    blk.overrides[param] = value

    def _status(self, blk: Block | None) -> Mapping[str, Any]:
        return blk.signals() if blk is not None else {}

    def signals(
        self, t: float, state: Mapping[str, Mapping[str, Value]], lead: Value
    ) -> dict[str, float | bool | str]:
        out: dict[str, float | bool | str] = {}
        for function, signal in (("dp_pid", "dp_pid_output"), ("bypass_pid", "bypass_pid_output")):
            status = self._status(self.blocks.get(function))
            if "output" in status:
                out[signal] = 100.0 * float(status["output"])

        staging = self.blocks.get("chw_staging")
        stage = self._status(staging)
        capacity = sum(getattr(staging, "capacity", {}).values()) if staging else 0.0
        load = stage.get("load_kW")
        if load is not None:
            out["cooling_load_demand"] = float(load)
            out["plant_load"] = 100.0 * float(load) / capacity if capacity > 0 else 0.0
        on: Sequence[str] = list(getattr(staging, "on", []))
        chillers: Sequence[str] = list(getattr(staging, "chillers", []))
        up = float(getattr(staging, "up_timer", 0.0))
        down = float(getattr(staging, "down_timer", 0.0))
        available = [c for c in chillers if not state.get(c, {}).get("tripped")]
        idle = [c for c in available if c not in on]
        out |= {
            "staging_pending": up > 0 or down > 0,
            "required_chillers": len(on) + (1 if up > 0 else 0) - (1 if down > 0 else 0),
            "next_to_start": _name(self.doc, idle[0]) if idle else "",
            "next_to_stop": _name(self.doc, on[-1]) if len(on) > 1 else "",
            "running_available": f"{len(on)}/{len(available)}",
            "loop_status": "RUNNING" if on else "STOPPED",
        }

        if isinstance(lead, str) and lead != self.lead:
            if self.lead is not None:
                self.rotations += 1
                self.last_rotation = f"{_name(self.doc, lead)} at {t / 3600:.1f} h"
            self.lead = lead
        out |= {
            "rotation_status": "MONITORING" if "rotation" in self.blocks else "DISABLED",
            "rotation_count": self.rotations,
            "last_rotation": self.last_rotation,
            "last_rotation_key": f"rotation-{self.rotations}",
        }

        alarming = [a for a in self.equipment if state.get(a, {}).get("alarm") is True]
        self.alarms = [a for a in self.alarms if a in alarming]
        self.alarms += [a for a in alarming if a not in self.alarms]
        halted = bool(state.get(self.controller, {}).get("halt"))
        normal = not self.alarms and not halted
        out |= {
            "alarms_unacknowledged": len(self.alarms),
            "latest_alarm_message": (
                f"{_name(self.doc, self.alarms[-1])} in alarm"
                if self.alarms
                else "No active alarms"
            ),
            "operating_mode": "HALT" if halted else "AUTO",
            "system_status": "NORMAL" if normal else "ALARM",
            "system_normal": normal,
        }
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "rotations": self.rotations,
            "last_rotation": self.last_rotation,
            "lead": self.lead,
            "alarms": list(self.alarms),
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        self.rotations = int(s.get("rotations", 0))
        self.last_rotation = str(s.get("last_rotation", ""))
        self.lead = s.get("lead")
        self.alarms = list(s.get("alarms", []))

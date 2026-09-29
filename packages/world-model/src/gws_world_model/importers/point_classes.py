"""Point classes and support scope for the graphene Ignition project.

These are judgements about the graphene export's naming, not facts in it, so they live in one
reviewable table. The first matching rule wins; anything unmatched is a process value.
"""

from __future__ import annotations

import re

from gws_world_model.model import PointClass

SUPPORT_FOLDERS: tuple[str, ...] = (
    "Smart Alarm Logic",
    "Testing",
    "Meter/decoder1",
    "Meter/GEM630CTL",
    "Breaker",
    "Line",
    "Temperature_Controls",
    "Level_Monitoring",
    "Pressure System",
    "MQTT Tags",
    "PredictionCache",
    "Device Card Abbreviation",
)
"""Export folders (anchored at the root) that hold Ignition-side artefacts with no physical
counterpart. Root `Breaker` and `Line` are not the Demo Rack's `DemoRack/Breaker*`."""


def in_support_folder(path: str) -> bool:
    return any(path == f or path.startswith(f"{f}/") for f in SUPPORT_FOLDERS)


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns))


_NETWORK_TYPES = frozenset({"Network Device", "Network Switch", "Network Switch Port"})
_STATIC_METADATA = _any(
    r"^(Equipment Name|Description|Rack ID|_Name|Outgoing No|Cable Length)$",
    r"_RackID$",
    r"^GEM630CTL_",
    r"^Maintenance Schedule$",
    r"^(Admin Status|Port Count|Staging Strategy|Minimum Flow Rate)$",
    r"^New Tag$",
)
_ENERGY_INTEGRAL = _any(
    r"Wh_Im",
    r"Energy",
    r"Totalizer",
    r"Run Hours|Operating Hours|Engine Run Time",
    r"^System Start Times$",
    r"^Rotation Count$",
)
_NETWORK_STATE = _any(r"^Comm$", r"^Gateway \d+ Status$")
_FAULT_ALARM = _any(
    r"(?i:alarm|fault|fail|trip|shutdown|warning|problem)",
    r"^(EF|Emergency Stop|Low Coolant Level)$",
    r"^(No CT|Short CT|PE Connection|Transformer Temp)$",
)
_FAULT_ALARM_FOLDERS = ("Fire Protection System/", "Chiller System Control/Alarms/")
_EQUIPMENT_STATE_FIRST = _any(r"^Staging Command Pending$", r"Mode Status$")
_COMMAND = _any(
    r"Auto_Manual",
    r"Command",
    r"Setpoint|\(SP\)| SP$",
    r"^(Enabled|Loop Enable)$",
    r"^(Manual Output|Control Variable)$",
    r"Speed Control$|Valve Control$",
    r"^Run_Stop",
    r"^Engine Start$",
    r"^(Chiller Load Limit|Pump Minimum Speed|Demo Cooling Demand)$",
    r"^(Minimum|Maximum) Chillers$",
    r"^Minimum (DP|Flow Rate)$",
    r"^Stage (Up|Down)( Inhibit)? Wait Time$",
    r"^(Staging Strategy|Selected Lead)$",
)
_FEEDBACK = _any(
    r"Feedback",
    r"(Open|Close) Status$",
    r"On_?Off",
    r"Run(ning)? Status$",
    r"^Position$",
    r"Fan Speed$",
    r"Compressor( 2)? Capacity$",
    r"^Output Frequency$",
)
_DRIVE_FEEDBACK_FOLDERS = ("Chiller System Control/Pumps/", "Chiller_System/Chillers/")
_EQUIPMENT_STATE = _any(
    r"Status|State|Mode$",
    r"^Ready To ",
    r"^(Idling|Operation|System Normal|Maintenance Due)$",
    r"^Chiller Buffer Tank Recharge$",
    r"^(Running|Required) Chillers$",
    r"^(Running Available|Next To Start|Next To Stop)$",
    r"^(Current Lead|Last Rotation|Last Scheduled Key)$",
    r"^(Direction|Lift Level|Moving Until)$",
)


def classify(
    *,
    path: str,
    name: str,
    type_id: str | None,
    data_type: str,
    value_source: str,
    unit: str | None,
    support: bool,
) -> PointClass:
    if support:
        return PointClass.SUPPORT
    if data_type in ("DataSet", "Document") or _STATIC_METADATA.search(name):
        return PointClass.STATIC_METADATA
    if path.startswith("Chiller System Control/Rotation Schedule/") and name in ("Time", "Enabled"):
        return PointClass.STATIC_METADATA
    if (type_id in _NETWORK_TYPES and name != "Temperature") or _NETWORK_STATE.search(name):
        return PointClass.NETWORK_STATE
    if unit == "kWh" or _ENERGY_INTEGRAL.search(name):
        return PointClass.ENERGY_INTEGRAL
    if path.startswith(_FAULT_ALARM_FOLDERS) or _FAULT_ALARM.search(name):
        return PointClass.FAULT_ALARM
    if type_id == "Water Leak Cable Sensor" and name == "Status":
        return PointClass.FAULT_ALARM
    if _EQUIPMENT_STATE_FIRST.search(name):
        return PointClass.EQUIPMENT_STATE
    if _COMMAND.search(name) or "/Commands/" in path:
        return PointClass.COMMAND
    if name.endswith("Mode") and value_source != "expr":
        return PointClass.COMMAND
    if _FEEDBACK.search(name):
        return PointClass.FEEDBACK
    if name in ("Speed", "Frequency") and path.startswith(_DRIVE_FEEDBACK_FOLDERS):
        return PointClass.FEEDBACK
    if type_id == "Cooling Tower" and name == "Frequency":
        return PointClass.FEEDBACK
    if _EQUIPMENT_STATE.search(name):
        return PointClass.EQUIPMENT_STATE
    return PointClass.PROCESS_VALUE

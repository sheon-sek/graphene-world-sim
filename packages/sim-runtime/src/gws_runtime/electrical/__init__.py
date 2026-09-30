"""Electrical domain: the site's single-line as a pandapower AC load flow, with the equipment
state that changes it (breakers, gensets, UPS batteries, transfer switches).

The master drives it once per macro step: `set_demand` for every load, then `step`, then it
reads `supply` (per-unit voltage for the equipment models) and `signals` (true state).
Changeover logic lives in the runtime controller blocks of `gws_runtime.controllers.electrical`,
which command this network through `command`.
"""

from gws_runtime.electrical.equipment import (
    BEHAVIOUR_FEEDER,
    BEHAVIOUR_GENSET,
    BEHAVIOUR_TRANSFER_SWITCH,
    BEHAVIOUR_UPS,
    BEHAVIOUR_UTILITY,
    UnknownCommand,
)
from gws_runtime.electrical.network import ElectricalNetwork

__all__ = [
    "BEHAVIOUR_FEEDER",
    "BEHAVIOUR_GENSET",
    "BEHAVIOUR_TRANSFER_SWITCH",
    "BEHAVIOUR_UPS",
    "BEHAVIOUR_UTILITY",
    "ElectricalNetwork",
    "UnknownCommand",
]

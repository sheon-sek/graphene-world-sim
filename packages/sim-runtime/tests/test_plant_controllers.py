"""Chiller plant controller blocks, driven through a fake signal bus."""

from __future__ import annotations

from pathlib import Path

import pytest

import gws_runtime.controllers.plant  # noqa: F401  (registers the blocks)
from gws_runtime import controllers
from gws_runtime.controllers import Block
from gws_runtime.values import Value
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import WorldModel

ROOT = Path(__file__).resolve().parents[3]


class Bus:
    def __init__(self, readings: dict[str, Value]) -> None:
        self.readings = readings
        self.writes: dict[str, Value] = {}

    def read(self, reference: str) -> Value:
        return self.readings.get(reference, self.writes.get(reference))

    def write(self, reference: str, value: Value) -> None:
        self.writes[reference] = value


@pytest.fixture(scope="module")
def doc() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


@pytest.fixture
def blocks(doc: WorldModel) -> dict[str, Block]:
    built, missing = controllers.build(doc, lambda b: b.controller == "~PLC-01")
    assert missing == []
    return {b.function: b for b in built}


def test_dp_loop_slows_the_pump_when_pressure_is_high_and_holds_on_a_bad_reading(
    blocks: dict[str, Block],
) -> None:
    dp = blocks["dp_pid"]
    bus = Bus({"HDR/DPS-01": 200.0})
    for _ in range(20):
        dp.step(0.0, 5.0, bus)
    slowed = bus.writes["Chiller/R_CP9:speed"]
    assert isinstance(slowed, float) and slowed < 0.5
    bus.readings["HDR/DPS-01"] = None
    dp.step(0.0, 5.0, bus)
    assert bus.writes["Chiller/R_CP9:speed"] == slowed


def test_staging_starts_the_lead_chiller_with_its_own_pumps_and_valves(
    blocks: dict[str, Block],
) -> None:
    staging = blocks["chw_staging"]
    assert sorted(staging.legs["Chiller/R_C1"]) == [  # type: ignore[attr-defined]
        "Chiller/R_CP1:speed",
        "Chiller/R_CP5:speed",
        "Chiller/R_CV1:position",
        "Chiller/R_CV5:position",
        "Chiller/R_CV9:position",
    ]
    status = {r: True for r in staging.status_refs}  # type: ignore[attr-defined]
    bus = Bus({"CH-001/TS-01": 20.0, "CH-001/TS-02": 14.0, "CH-001/FM-01": 100.0, **status})
    staging.step(0.0, 5.0, bus)
    assert bus.writes["Chiller/R_C1:enable"] is True
    assert bus.writes["Chiller/R_C2:enable"] is False
    assert bus.writes["Chiller/R_CP1:speed"] == 1.0


def test_staging_replaces_a_tripped_chiller(blocks: dict[str, Block]) -> None:
    staging = blocks["chw_staging"]
    status = {r: True for r in staging.status_refs}  # type: ignore[attr-defined]
    bus = Bus({"CH-001/TS-01": 20.0, "CH-001/TS-02": 14.0, "CH-001/FM-01": 100.0, **status})
    staging.step(0.0, 5.0, bus)
    bus.readings[staging.status_refs[0]] = False  # type: ignore[attr-defined]  # R_C1 stops: it tripped
    for _ in range(3):
        staging.step(0.0, 5.0, bus)
    assert bus.writes["Chiller/R_C2:enable"] is True
    assert bus.writes["Chiller/R_CP2:speed"] == 1.0


def test_supply_temperature_trim_follows_the_header_reading(blocks: dict[str, Block]) -> None:
    chw = blocks["chw_supply_temp"]
    bus = Bus({"HDR/TS-02": 15.0})  # a degree warm: trim the chillers colder
    for _ in range(10):
        chw.step(0.0, 60.0, bus)
    setpoint = bus.writes["Chiller/R_C1:TChwSet"]
    assert isinstance(setpoint, float) and setpoint < 14.0 + 273.15


def test_hmi_settings_start_at_the_configured_values_and_an_operator_write_takes_over(
    doc: WorldModel, blocks: dict[str, Block]
) -> None:
    from gws_runtime.controllers.hmi import PlantHmi

    hmi = PlantHmi.build("~PLC-01", doc, blocks.values())
    register = hmi.defaults()
    configured = blocks["dp_pid"].binding.parameters["setpoint_kPa"]
    assert register["~PLC-01:chw_dp_set"] == configured
    assert hmi.writable("chw_dp_set") and not hmi.writable("plant_load")

    dp = blocks["dp_pid"]
    bus = Bus({"HDR/DPS-01": float(configured)})
    register["~PLC-01:chw_dp_set"] = float(configured) * 2
    hmi.apply(register)
    for _ in range(20):
        dp.step(0.0, 5.0, bus)
    raised = bus.writes["Chiller/R_CP9:speed"]
    assert isinstance(raised, float) and raised > 0.5  # pressure now far under the new set point

    register["~PLC-01:dp_pid_mode"] = "MANUAL"
    register["~PLC-01:dp_pid_manual_output"] = 40.0
    hmi.apply(register)
    dp.step(0.0, 5.0, bus)
    assert bus.writes["Chiller/R_CP9:speed"] == pytest.approx(0.4)

    status = hmi.signals(0.0, {}, None)
    assert status["dp_pid_output"] == pytest.approx(40.0)
    assert status["system_status"] == "NORMAL"
    assert status["latest_alarm_message"] == "No active alarms"


def test_hmi_registers_read_in_the_units_the_hmi_shows() -> None:
    from gws_runtime.controllers.hmi import CONFIGURATION, SETTINGS, UNITS

    status = {"dp_pid_output", "bypass_pid_output", "cooling_load_demand", "plant_load"}
    assert set(UNITS) <= set(SETTINGS) | set(CONFIGURATION) | status
    assert UNITS["chw_supply_temp_set"] == "degC" and UNITS["tower_approach_set"] == "dK"


def test_plant_load_is_the_heat_the_chillers_take_out_of_the_water(
    doc: WorldModel, blocks: dict[str, Block]
) -> None:
    """#83: the load is each chiller's flow across its own entering and leaving water, not the
    header's difference across the secondary loop's flow."""
    from gws_runtime.controllers.hmi import UNITS, PlantHmi

    staging = blocks["chw_staging"]
    status = {r: True for r in staging.status_refs}  # type: ignore[attr-defined]
    # R_C1 at its design flow, 0.47 K across it; R_C2 off, its meters still reading.
    readings = {"CH-001/TS-01": 14.36, "CH-001/TS-02": 13.89, "CH-001/FM-01": 603.8}
    readings |= {"CH-002/TS-01": 14.4, "CH-002/TS-02": 14.4, "CH-002/FM-01": 0.0}
    bus = Bus({**readings, **status})
    staging.step(0.0, 5.0, bus)
    load = staging.signals()["load_kW"]
    assert load == pytest.approx(603.8 * 4.184 * 0.47 / 3.6, rel=1e-6)  # about 330 kW

    hmi = PlantHmi.build("~PLC-01", doc, blocks.values())
    out = hmi.signals(0.0, {}, None)
    assert out["cooling_load_demand"] == pytest.approx(load)
    assert out["plant_load"] == pytest.approx(100.0 * load / 3500.0)  # of the running R_C1
    assert UNITS["cooling_load_demand"] == "kW" and UNITS["plant_load"] == "%"


def test_staging_wired_the_pre_83_way_names_the_fix_instead_of_failing_every_step(
    doc: WorldModel,
) -> None:
    staging = next(b for b in doc.control_bindings.values() if b.function == "chw_staging")
    statuses = staging.reads[-4:]
    old = staging.model_copy(update={"reads": ("HDR/TS-01", "HDR/TS-02", "HDR/FM-01", *statuses)})
    with pytest.raises(ValueError, match="import it again"):
        controllers.BLOCKS["chw_staging"](old, doc)

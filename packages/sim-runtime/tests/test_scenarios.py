"""The incident scenario set (#22): every event names something the model can do, and a
scenario's data is reproducible from its event log."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from gws_runtime.compiler import CACHE, plan
from gws_runtime.scenarios import ScenarioSet, run, schedule, scope
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import WorldModel

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ScenarioSet.read(ROOT / "data" / "scenarios" / "incidents.json")


@pytest.fixture(scope="module")
def doc() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


def test_every_fault_names_a_mode_its_target_has(doc: WorldModel) -> None:
    for scenario in SCENARIOS.scenarios:
        assert scenario.events and scenario.truth["root_cause"], scenario.id
        for event in scenario.events:
            if event.kind != "fault":
                continue
            target, mode = event.payload["target"], event.payload["mode"]
            if target in doc.assets:
                assert mode in doc.component_types[doc.assets[target].type].fault_modes, scenario.id
            else:
                assert target in doc.instruments or target in doc.point_bindings, scenario.id
        for cause in scenario.truth["root_cause"]:
            assert cause["asset"] in doc.assets or cause["asset"] in doc.instruments, scenario.id


def test_events_follow_the_warm_up_in_order() -> None:
    scenario = SCENARIOS.get("fouling-and-tower-fan")
    steps = [step for step, _, _ in schedule(SCENARIOS, scenario)]
    start = SCENARIOS.steps(SCENARIOS.warmup_s)
    assert steps == [0, start, start + SCENARIOS.steps(300)]


def _scope_available(doc: WorldModel) -> bool:
    if all((CACHE / f"{p.name}.fmu").exists() for p in plan(doc, scope(doc)).partitions):
        return True
    return shutil.which("docker") is not None and "GWS_OMLIB" in os.environ


def test_a_scenario_replays_to_the_same_data(doc: WorldModel) -> None:
    if not _scope_available(doc):
        pytest.skip("needs OpenModelica or a cached slice FMU")
    short = replace(SCENARIOS, warmup_s=20.0)
    session = run(doc, short, short.get("chiller-trip"), seed=3, until_s=60.0)
    frame = session.sim.frame().to_json()
    assert frame["points"]["Chiller/R_C1/System Failure_Trip"]["value"] in (1, True)
    assert frame["points"]["Chiller/R_C2/On_Off"]["value"] in (1, True)  # the standby (#84)
    assert session.replay().sim.frame().to_json() == frame
    session.close()


def test_a_restart_counts_as_one_start(doc: WorldModel) -> None:
    if not _scope_available(doc):
        pytest.skip("needs OpenModelica or a cached slice FMU")
    short = replace(SCENARIOS, warmup_s=20.0)
    session = run(doc, short, short.get("utility-loss"), until_s=300.0)
    starts = session.sim.frame().to_json()["points"]["Chiller/R_C1/System Start Times"]["value"]
    assert 0 <= starts <= 2
    session.close()

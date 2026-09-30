"""The master, lifecycle and fault framework on a scope with no thermofluid model (no
OpenModelica needed): one data hall's IT load on its UPS and generator supply path."""

from __future__ import annotations

from pathlib import Path

import pytest

from gws_runtime.faults import Fault, FaultBook
from gws_runtime.lifecycle import Session
from gws_runtime.master import RuntimeProblem, Simulation
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import FaultKind, WorldModel

ROOT = Path(__file__).resolve().parents[3]
IT = "~IT-DH01"
SCOPE = [IT, "UPS/UPS 1"]
IT_POINT = "Dashboard/Data Halls/DH01/IT Load"


@pytest.fixture(scope="module")
def doc() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


def test_scope_brings_its_supply_path_and_the_controllers_on_it(doc: WorldModel) -> None:
    sim = Simulation(doc, SCOPE)
    assert sim.plan.partitions == ()
    assert {"~ATS-A", "Genset/Genset 1", "UPS/UPS 1"} <= sim.electrical.assets
    assert "ATS-A/ats" in [b.binding.id for b in sim.blocks]
    frame = sim.run(2)
    assert frame.state[IT]["P"] == pytest.approx(447.5e3, rel=1e-3)
    assert frame.points[IT_POINT].value == pytest.approx(447.5)


def test_utility_loss_rides_through_on_ups_until_the_gensets_take_over(doc: WorldModel) -> None:
    sim = Simulation(doc, SCOPE)
    sim.run(2)
    sim.set_conditions({"utility_available": False})
    sources = []
    for _ in range(30):
        frame = sim.step()
        assert frame.state[IT]["P"] > 0  # never dropped
        sources.append(sim.state["~ATS-A"]["source"])
    assert sources[0] == 1.0 and sources[-1] == 2.0
    assert sim.state["UPS/UPS 1"]["soc"] < 1.0  # the battery carried the gap


def test_trip_latches_until_reset(doc: WorldModel) -> None:
    sim = Simulation(doc, SCOPE)
    fault = sim.inject(IT, "emergency_power_off")
    assert fault.latching
    assert sim.step().state[IT]["P"] == 0
    sim.clear(fault.id)
    assert sim.step().state[IT]["tripped"] is True
    assert sim.reset(IT) == [fault.id]
    sim.step()
    assert sim.step().state[IT]["P"] > 0


def test_sensor_fault_changes_the_reading_not_the_physics(doc: WorldModel) -> None:
    sim = Simulation(doc, SCOPE)
    sim.inject(IT_POINT, "bias", {"bias": 10.0})
    frame = sim.run(2)
    assert frame.points[IT_POINT].value == pytest.approx(457.5)
    assert frame.state[IT]["P"] == pytest.approx(447.5e3, rel=1e-3)


def test_faults_are_refused_when_the_type_does_not_declare_them(doc: WorldModel) -> None:
    sim = Simulation(doc, SCOPE)
    with pytest.raises(RuntimeProblem, match="no fault mode"):
        sim.inject(IT, "melt")
    with pytest.raises(RuntimeProblem):
        sim.command(IT, "speed", 1.0)


def test_replay_reproduces_the_trajectory(doc: WorldModel) -> None:
    session = Session(doc, SCOPE, seed=7)
    session.step(3)
    session.apply("conditions", {"changes": {"utility_available": False}})
    session.step(8)
    fault = session.apply("fault", {"target": IT, "mode": "emergency_power_off"})
    session.step(2)
    session.apply("clear", {"id": fault.id})
    session.apply("reset", {"target": IT})
    session.apply("conditions", {"changes": {"it_fraction": {"DH01": 0.8}}})
    final = session.step(10).to_json()

    again = session.replay()
    assert again.sim.step_count == session.sim.step_count
    assert again.sim.frame().to_json() == final


def test_snapshot_restore_continues_identically(doc: WorldModel) -> None:
    session = Session(doc, SCOPE)
    session.apply("conditions", {"changes": {"utility_available": False}})
    session.step(5)
    snap = session.snapshot("before transfer")
    first = session.step(15).to_json()
    session.restore(snap.id)
    assert session.sim.step_count == 5
    assert session.step(15).to_json() == first


def test_fault_severity_ramp_and_auto_clear() -> None:
    book = FaultBook()
    f = book.add(
        Fault(
            "",
            "Chiller/R_C1",
            "capacity_loss",
            FaultKind.DEGRADE,
            "thermofluid",
            {"capacity_fraction": 0.6},
            severity=0.5,
            ramp_s=100.0,
            duration_s=300.0,
        )
    )
    assert f.values(0.0) == {"capacity_fraction": 1.0}
    assert f.values(50.0)["capacity_fraction"] == pytest.approx(0.9)
    assert f.values(500.0)["capacity_fraction"] == pytest.approx(0.8)
    assert book.expire(299.0) == []
    assert book.expire(300.0) == [f] and book.active() == []

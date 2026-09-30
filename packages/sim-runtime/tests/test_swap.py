"""Structural edits applied without stopping (#21), and rolled back when they fail (#67)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gws_runtime.compiler import CACHE, plan
from gws_runtime.gate import SLICE
from gws_runtime.lifecycle import Session
from gws_runtime.master import Simulation
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import WorldModel
from gws_world_model.ops import Place, Put, apply

ROOT = Path(__file__).resolve().parents[3]
SCOPE = ["~IT-DH01", "UPS/UPS 1"]
NEW = "UPS/UPS 99"


@pytest.fixture(scope="module")
def revisions() -> dict[int, WorldModel]:
    doc = build(Sources.read(ROOT / "data" / "graphene"))
    ups = doc.assets["UPS/UPS 1"].model_dump(mode="json")
    feed = doc.connections["power:Meter/Level 2_MSB A_2->UPS/UPS 1"].model_dump(mode="json")
    feed["id"], feed["target"]["node"] = f"power:Meter/Level 2_MSB A_2->{NEW}", NEW
    grown = apply(
        doc,
        [
            Place(value=ups | {"id": NEW, "name": "UPS 99"}),
            Put(collection="connections", value=feed),
        ],
    )
    dangling = feed | {"id": "power:broken", "target": {"node": "UPS/Nowhere", "port": "power_in"}}
    broken = apply(doc, [Put(collection="connections", value=dangling)])
    return {1: doc, 2: grown, 3: broken}


def _session(revisions: dict[int, WorldModel], scope: list[str]) -> Session:
    return Session(revisions[1], scope, revision=1, resolve=revisions.__getitem__)


def test_a_swap_lands_between_steps_and_replays(revisions: dict[int, WorldModel]) -> None:
    session = _session(revisions, SCOPE)
    session.step(3)
    swap = session.prepare(2)
    assert (swap.state, swap.added, swap.compiling) == ("ready", (NEW,), ())
    session.step(2)
    assert session.revision == 1
    frame = session.apply_swap()
    assert frame is not None and swap.state == "applied" and swap.applied_t == 5.0
    assert session.revision == 2 and NEW in session.scope
    assert frame.points[f"{NEW}/Average Input Voltage"].quality.value == "good"
    assert session.events[-1].kind == "reinit"
    final = session.step(4).to_json()
    assert session.replay().sim.frame().to_json() == final


def test_a_broken_edit_leaves_the_session_running(
    revisions: dict[int, WorldModel], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _session(revisions, SCOPE)
    swap = session.prepare(3)
    assert swap.state == "failed" and "UPS/Nowhere" in swap.reason
    assert session.apply_swap() is None

    def boom(self: Simulation, snap: object) -> None:
        raise RuntimeError("initialisation failed")

    old = session.sim
    session.prepare(2)
    monkeypatch.setattr(Simulation, "restore", boom)
    assert session.apply_swap() is None
    assert session.swap is not None and session.swap.reason == "initialisation failed"
    monkeypatch.undo()
    assert session.sim is old and session.revision == 1
    assert session.step(2).step == 2
    assert [e.kind for e in session.events] == ["init"]


def _slice_available(doc: WorldModel) -> bool:
    if all((CACHE / f"{p.name}.fmu").exists() for p in plan(doc, SLICE).partitions):
        return True
    return shutil.which("docker") is not None and "GWS_OMLIB" in os.environ


def test_unchanged_partitions_keep_running_through_a_swap(
    revisions: dict[int, WorldModel],
) -> None:
    if not _slice_available(revisions[1]):
        pytest.skip("needs OpenModelica or a cached slice FMU")
    session = _session(revisions, list(SLICE))
    session.step(2)
    running = dict(session.sim.fmus)
    session.prepare(2)
    assert session.apply_swap() is not None
    assert session.sim.adopted == frozenset(running)
    assert all(session.sim.fmus[name] is unit for name, unit in running.items())
    session.step(2)
    session.close()

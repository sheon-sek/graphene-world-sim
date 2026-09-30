"""The Phase 6 coverage gate: every Asset Model point has a source, no evidence point is a
constant, and without the thermofluid models every point that does not depend on them reads."""

from __future__ import annotations

from pathlib import Path

import pytest

import gws_runtime.master as master
from gws_runtime.compiler import Plan, plan
from gws_runtime.values import Quality, split_ref
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import (
    Aggregate,
    AssetSignal,
    InstrumentSource,
    PointClass,
    StaticValue,
    Unbound,
    WorldModel,
)

ROOT = Path(__file__).resolve().parents[3]

CONSTANT_CLASSES = {PointClass.STATIC_METADATA, PointClass.SUPPORT}
"""Point classes that may be served as a fixed value."""

ASSUMPTIONS = {
    "Dashboard/GFA": "the building's floor area",
    "Dashboard/Carbon Footprint/Demo Inputs/Capital Goods Emission Rate": "carbon assumption",
    "Dashboard/Carbon Footprint/Demo Inputs/Refrigerant Emission Rate": "carbon assumption",
    "Dashboard/Carbon Footprint/Demo Inputs/Fuel and Energy Related Emission Rate": (
        "carbon assumption"
    ),
    "Dashboard/Carbon Footprint/Demo Inputs/Year Start": "reporting-year origin",
    "Dashboard/Carbon Footprint/Scope 1/Diesel Emission Factor": "published factor",
    "Dashboard/Carbon Footprint/Scope 2/Grid Emission Factor": "published factor",
    "Other/Maintenance Due": "the runtime has no calendar to compare the schedule with",
}
"""Evidence points that are fixed inputs by nature, not measurements, and why."""

THERMOFLUID_CONTROLLERS = {"Chiller Plant Controller"}
"""Controllers whose status follows the thermofluid plant they run."""


@pytest.fixture(scope="module")
def doc() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


def test_every_point_has_a_source(doc: WorldModel) -> None:
    unbound = [p for p, b in doc.point_bindings.items() if isinstance(b.source, Unbound)]
    assert unbound == []


def test_no_evidence_point_is_a_constant(doc: WorldModel) -> None:
    constant = [
        p
        for p, b in doc.point_bindings.items()
        if isinstance(b.source, StaticValue)
        and b.point_class not in CONSTANT_CLASSES
        and p not in ASSUMPTIONS
    ]
    assert constant == []
    assert set(ASSUMPTIONS) <= set(doc.point_bindings)


def test_without_the_thermofluid_models_every_other_point_reads(
    doc: WorldModel, monkeypatch: pytest.MonkeyPatch
) -> None:
    modelled = plan(doc, doc.assets.keys()).modelled
    rooms = {doc.assets[a].location.room for a in modelled if a in doc.assets}

    def thermofluid(asset: str) -> bool:
        a = doc.assets[asset]
        return asset in modelled or a.location.room in rooms or a.type in THERMOFLUID_CONTROLLERS

    def depends(reference: str, seen: frozenset[str] = frozenset()) -> bool:
        binding = doc.point_bindings.get(reference)
        if binding is None and reference in doc.instruments:
            return thermofluid(doc.instruments[reference].asset)
        if binding is None:
            return thermofluid(split_ref(reference)[0])
        src = binding.source
        if isinstance(src, AssetSignal):
            return thermofluid(src.asset)
        if isinstance(src, InstrumentSource):
            return thermofluid(doc.instruments[src.instrument].asset)
        if isinstance(src, Aggregate):
            return any(depends(r, seen | {reference}) for r in src.inputs if r not in seen)
        return False

    monkeypatch.setattr(master, "plan", lambda doc, scope: Plan((), frozenset(), {}, ()))
    sim = master.Simulation(doc, doc.assets.keys(), dt=1.0)
    frame = sim.run(3)
    assert set(frame.points) == set(doc.point_bindings)
    unexplained = sorted(
        f"{p}: {s.reason}"
        for p, s in frame.points.items()
        if s.quality is not Quality.GOOD and not depends(p)
    )
    assert unexplained == []

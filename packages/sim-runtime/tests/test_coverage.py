"""The Phase 6 coverage gate: every Asset Model point has a source, no evidence point is a
constant, and without the thermofluid models every point that does not depend on them reads."""

from __future__ import annotations

from pathlib import Path

import pytest

import gws_runtime.master as master
from gws_runtime.behaviours import BEHAVIOURS
from gws_runtime.compiler import Plan, plan
from gws_runtime.services import SiteServices
from gws_runtime.values import Quality, split_ref
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import (
    Aggregate,
    AssetSignal,
    Domain,
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


RUNTIME_SIGNALS = {"tripped", "alarm", "run_hours", "starts_day", "short_cycling"}
"""Signals the runtime keeps for every modelled unit, beside its model's."""
RETURN_AIR = {"TRetAir", "phiRet"}
"""What a unit that supplies a room reads of that room's air, when its model has no return."""


def test_every_thermofluid_point_names_a_signal_its_unit_reports(doc: WorldModel) -> None:
    modelled = plan(doc, doc.assets.keys()).modelled
    bound: dict[str, set[str]] = {}
    for binding in doc.point_bindings.values():
        if isinstance(binding.source, AssetSignal):
            bound.setdefault(binding.source.asset, set()).add(binding.source.signal)

    powered = {c.target.node for c in doc.connections.values() if c.domain is Domain.POWER}
    towers = {
        c
        for c, kind in SiteServices.from_world(doc, doc.assets.keys()).water.consumers.items()
        if kind == "tower"
    }
    supplies = {
        c.source.node
        for c in doc.connections.values()
        if c.source.port == "air_out" and c.target.is_room
    }

    def reports(asset: str, signal: str) -> bool:
        a = doc.assets[asset]
        ctype = doc.component_types[a.type]
        b = BEHAVIOURS[ctype.behaviour or ""]
        members = set(ctype.point_template) | bound.get(asset, set())
        electrical = master.electrical_points(members, a.type)
        alias = b.points.get(signal) or electrical.get(signal) or signal
        name = alias.removeprefix("on:").removeprefix("energy:")
        own = set(b.outputs) | set(b.inputs) | set(b.derived) | set(b.settings)
        return (
            name in own
            or name in RUNTIME_SIGNALS
            or (name in RETURN_AIR and asset in supplies)
            or (name in master.ELECTRICAL_UNITS and asset in powered)
            or (name == "basinLevel" and asset in towers)
            or alias in master.OPERATOR_SIGNALS | {master.COMM_LOST}
            or signal in electrical
        )

    references: list[tuple[str, str, str]] = []
    for p, b in doc.point_bindings.items():
        if isinstance(b.source, AssetSignal):
            references.append((b.source.asset, b.source.signal, p))
        elif isinstance(b.source, Aggregate):
            for r in b.source.inputs:
                if r not in doc.point_bindings and r not in doc.instruments:
                    asset, signal = split_ref(r)
                    references.append((asset, signal, p))
    unreported = sorted(
        f"{asset}:{signal} ({p})"
        for asset, signal, p in references
        if asset in modelled and not reports(asset, signal)
    )
    assert unreported == []

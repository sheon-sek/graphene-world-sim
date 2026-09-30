from __future__ import annotations

from pathlib import Path

import pytest

from gws_world_model import library
from gws_world_model.importers.graphene import (
    Bindings,
    ImportProblem,
    Sources,
    Supplement,
    build,
    contract_checksum,
)
from gws_world_model.model import AssetSignal, WorldModel
from gws_world_model.validate import validate

ROOT = Path(__file__).resolve().parents[3]
CONTRACT = "2d3c608ac2a62f97179b98a38baa130d848f8805b6ea312715fda203500435a0"


@pytest.fixture(scope="module")
def sources() -> Sources:
    return Sources.read(ROOT / "data" / "graphene")


@pytest.fixture(scope="module")
def doc(sources: Sources) -> WorldModel:
    return build(sources)


def test_library_files_are_named_by_type_id() -> None:
    types = library.load()
    assert len(types) == 65
    assert library.slug("Production/GPM96") == "production-gpm96"


def test_every_asset_and_connection_is_imported(doc: WorldModel) -> None:
    assert len(doc.assets) == 777
    assert sum(a.exported for a in doc.assets.values()) == 639
    assert len(doc.connections) == 800
    assert len(doc.site.rooms) == 33
    assert [f.id for f in doc.site.floors] == ["Ground", "Level 1", "Level 2", "Roof"]


def test_point_paths_data_types_and_type_ids_keep_the_ignition_contract(
    doc: WorldModel, sources: Sources
) -> None:
    assert len(doc.point_bindings) == 8811
    assert contract_checksum(doc) == sources.export.contract_checksum() == CONTRACT


def test_revision_one_validates_clean(doc: WorldModel) -> None:
    assert validate(doc) == []


def test_udt_members_bind_to_signals_their_type_exposes(doc: WorldModel) -> None:
    for p in doc.point_bindings.values():
        if isinstance(p.source, AssetSignal) and doc.assets[p.source.asset].exported:
            template = doc.component_types[doc.assets[p.source.asset].type].point_template
            assert template, p.path
            if p.path.startswith(f"{p.source.asset}/"):
                assert p.source.signal in template, p.path


def test_ports_follow_domain_and_direction(doc: WorldModel) -> None:
    c = doc.connections["chw:Chiller/R_CP1->Chiller/R_CV1"]
    assert (c.source.port, c.target.port) == ("chw_out", "chw_in")
    to_room = next(c for c in doc.connections.values() if c.target.is_room)
    assert to_room.target.port == to_room.domain


def test_hall_it_load_comes_from_the_design_basis(doc: WorldModel) -> None:
    dh01 = doc.conditions.it_load["DH01"]
    assert dh01.design_kw == 1000
    assert dh01.fraction == pytest.approx(0.4475)


def test_missing_library_type_is_reported(sources: Sources) -> None:
    types = {k: v for k, v in library.load().items() if k != "Chiller"}
    with pytest.raises(ImportProblem, match="no library type 'Chiller'"):
        build(sources, types, Bindings())


PLANT_VIEWS = (
    "Chiller System Control/",
    "Chiller_System/",
    "Dashboard/",
    "Other/",
    "Environment Monitoring/",
)


def test_every_plant_view_point_is_bound(doc: WorldModel) -> None:
    unbound = [
        p.path
        for p in doc.point_bindings.values()
        if p.source.kind == "unbound"
        and p.path.startswith(PLANT_VIEWS)
        and not p.path.startswith("Dashboard/Carbon Footprint/")  # demo inputs, no values
    ]
    assert unbound == []


def test_every_temperature_point_has_a_unit(doc: WorldModel) -> None:
    unitless = [
        p.path
        for p in doc.point_bindings.values()
        if p.path.endswith("Temperature") and isinstance(p.source, AssetSignal) and p.unit is None
    ]
    assert unitless == []
    assert doc.point_bindings["FCU/L1_FCU1/Return Air Temperature"].unit == "°C"
    assert doc.component_types["FCU"].point_template["Supply Air Temperature"].unit == "°C"


def test_a_supplement_never_overrides_a_unit_the_export_gives(sources: Sources) -> None:
    override = Supplement(note="test", member_units={"CRAC": {"Return Air Temperature": "K"}})
    with pytest.raises(ImportProblem, match="which has one"):
        build(sources, supplements=[*Supplement.load_all(), override])

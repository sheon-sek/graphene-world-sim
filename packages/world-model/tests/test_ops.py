from __future__ import annotations

from pathlib import Path

import pytest

from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import AssetSignal, WorldModel
from gws_world_model.ops import OperationError, Place, Remove, apply
from gws_world_model.validate import validate

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def site() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


def test_placing_an_exported_asset_binds_its_type_template(site: WorldModel) -> None:
    c1 = site.assets["Chiller/R_C1"].model_dump(mode="json")
    doc = apply(site, [Place(value=c1 | {"id": "Chiller/R_C9", "name": "R_C9"})])
    new = {p: b for p, b in doc.point_bindings.items() if p.startswith("Chiller/R_C9/")}
    old = {p: b for p, b in site.point_bindings.items() if p.startswith("Chiller/R_C1/")}
    assert {p.removeprefix("Chiller/R_C9/") for p in new} == {
        p.removeprefix("Chiller/R_C1/") for p in old
    }
    power = new["Chiller/R_C9/Input Power"]
    assert power.source == AssetSignal(asset="Chiller/R_C9", signal="Input Power")
    assert power.ignition_type_id == "Chiller"
    with pytest.raises(OperationError, match="already exists"):
        apply(site, [Place(value=c1)])


def test_removing_an_asset_takes_what_hangs_off_it(site: WorldModel) -> None:
    doc = apply(site, [Remove(key="Chiller/R_C1")])
    assert "Chiller/R_C1" not in doc.assets
    assert not [p for p in doc.point_bindings if p.startswith("Chiller/R_C1/")]
    assert not [i for i in doc.instruments.values() if i.asset == "Chiller/R_C1"]
    assert not [
        c for c in doc.connections.values() if "Chiller/R_C1" in (c.source.node, c.target.node)
    ]
    for b in doc.control_bindings.values():
        assert not any(r.startswith("Chiller/R_C1:") for r in (*b.reads, *b.drives))
    assert [i for i in validate(doc) if i.severity == "error"] == []

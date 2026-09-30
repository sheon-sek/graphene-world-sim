"""Ignition tags generated from point bindings (#18)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gws_api.app import create_app
from gws_api.ignition import FAULT_ALARM, TYPES_FOLDER, generate, item_path, resolve
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import WorldModel
from gws_world_model.store import SqliteStore

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def doc() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


def test_every_point_binding_is_one_tag_bound_to_its_node(doc: WorldModel) -> None:
    tags = resolve(generate(doc, "Sim"))
    assert {path: (server, item) for path, (server, item, _) in tags.items()} == {
        path: ("Sim", item_path(path)) for path in doc.point_bindings
    }
    voltage = tags["Genset/Genset 1/AC Voltage: L1-N"]
    assert (
        voltage[1] == "nsu=urn:eetarp:graphene:demo:twin;s=point:Genset/Genset 1/AC Voltage%3A L1-N"
    )
    kinds = {b.data_type: tags[p][2] for p, b in doc.point_bindings.items()}
    assert kinds["Float4"] == "Float4" and kinds["DataSet"] == "String"


def test_uniform_udt_types_become_definitions_with_instances(doc: WorldModel) -> None:
    document = generate(doc)
    types = next(t for t in document["tags"] if t["name"] == TYPES_FOLDER)
    names = {t["name"] for t in types["tags"]}
    assert {"Chiller", "Cooling Tower"} <= names
    chiller = next(t for t in document["tags"] if t["name"] == "Chiller")
    r_c1 = next(t for t in chiller["tags"] if t["name"] == "R_C1")
    assert r_c1["tagType"] == "UdtInstance" and r_c1["typeId"] == "Chiller"
    assert r_c1["parameters"]["PointPath"]["value"] == "Chiller/R_C1"


def test_boolean_fault_points_raise_an_alarm(doc: WorldModel) -> None:
    document = generate(doc)
    udt = next(t for t in document["tags"] if t["name"] == TYPES_FOLDER)
    chiller = next(t for t in udt["tags"] if t["name"] == "Chiller")
    trip = next(t for t in chiller["tags"] if t["name"] == "System Failure_Trip")
    assert trip["alarms"] == [FAULT_ALARM]
    assert "alarms" not in next(t for t in chiller["tags"] if t["name"] == "Input Power")
    quiet = generate(doc, alarms=False)
    assert "alarms" not in str(quiet)


def test_tags_record_history_to_a_named_provider(doc: WorldModel) -> None:
    document = generate(doc, history="Sim History")
    udt = next(t for t in document["tags"] if t["name"] == TYPES_FOLDER)
    power = next(
        t
        for t in next(t for t in udt["tags"] if t["name"] == "Chiller")["tags"]
        if t["name"] == "Input Power"
    )
    assert power["historyEnabled"] is True and power["historyProvider"] == "Sim History"
    assert "history" not in str(generate(doc))


def test_tags_endpoint_serves_the_head_revision(doc: WorldModel) -> None:
    store = SqliteStore()
    with TestClient(create_app(store)) as client:
        assert client.get("/api/ignition/tags").status_code == 409
        store.create_revision(doc, "import", "importer")
        body = client.get("/api/ignition/tags", params={"connection": "Twin"}).json()
        assert len(resolve(body)) == len(doc.point_bindings)
        assert client.get("/api/ignition/tags", params={"revision": 9}).status_code == 404
    store.close()

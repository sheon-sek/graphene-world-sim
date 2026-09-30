from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gws_api.app import create_app, openapi_json
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import WorldModel
from gws_world_model.store import SqliteStore

ROOT = Path(__file__).resolve().parents[3]
API = "/api/world-model"


@pytest.fixture(scope="module")
def revision_one() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


@pytest.fixture
def client(revision_one: WorldModel) -> Iterator[TestClient]:
    store = SqliteStore()
    store.create_revision(revision_one, "import graphene", "importer")
    with TestClient(create_app(store)) as client:
        yield client
    store.close()


def _draft(client: TestClient) -> str:
    response = client.post(f"{API}/drafts", json={"author": "sheon"})
    assert response.status_code == 201
    draft_id: str = response.json()["id"]
    return draft_id


def test_published_openapi_is_current() -> None:
    published = ROOT / "docs" / "api" / "openapi.json"
    assert published.read_text(encoding="utf-8") == openapi_json(), (
        "run: uv run python -m gws_api.app docs/api/openapi.json"
    )


def test_summary_counts_the_head_revision(client: TestClient) -> None:
    body = client.get(f"{API}/summary").json()
    assert body["revision"] == 1
    assert body["counts"]["assets"] == 777
    assert body["counts"]["point_bindings"] == 8811


def test_reads_types_assets_connections_and_points(client: TestClient) -> None:
    assert client.get(f"{API}/types/Production/GPM96").json()["id"] == "Production/GPM96"
    chillers = client.get(f"{API}/assets", params={"type": "Chiller"}).json()
    assert {a["id"] for a in chillers} >= {"Chiller/R_C1", "~CH-004"}
    assert client.get(f"{API}/assets/Chiller/R_C1").json()["type"] == "Chiller"
    links = client.get(f"{API}/connections", params={"node": "Chiller/R_C1"}).json()
    assert any(c["id"] == "chw:Chiller/R_CV1->Chiller/R_C1" for c in links)
    page = client.get(f"{API}/points", params={"prefix": "Chiller/R_C1/", "limit": 5}).json()
    assert page["total"] > 5 and len(page["items"]) == 5


def test_unknown_entities_are_404(client: TestClient) -> None:
    assert client.get(f"{API}/assets/Nope").status_code == 404
    assert client.get(f"{API}/revisions/9").status_code == 404
    assert client.get(f"{API}/drafts/nope").status_code == 404


def test_draft_edit_validate_diff_and_apply(client: TestClient) -> None:
    draft_id = _draft(client)
    asset = client.get(f"{API}/assets/Chiller/R_C1").json()
    asset["role"] = "Duty chiller 1"
    ops = [
        {"op": "put", "collection": "assets", "value": asset},
        {"op": "set_conditions", "value": {"weather": {"dry_bulb_c": 33.0}}},
    ]
    assert client.post(f"{API}/drafts/{draft_id}/operations", json=ops).status_code == 200
    assert client.get(f"{API}/drafts/{draft_id}/validation").json()["valid"] is True
    diff = client.get(f"{API}/drafts/{draft_id}/diff").json()
    assert diff["overall"] == "live"
    assert {c["key"] for c in diff["changes"]} == {"Chiller/R_C1", "conditions"}

    applied = client.post(f"{API}/drafts/{draft_id}/apply", json={"message": "rename role"})
    assert applied.status_code == 201
    assert applied.json()["number"] == 2
    assert client.get(f"{API}/assets/Chiller/R_C1").json()["role"] == "Duty chiller 1"
    assert client.get(f"{API}/assets/Chiller/R_C1", params={"revision": 1}).json()["role"] != (
        "Duty chiller 1"
    )
    assert client.get(f"{API}/revisions/1/diff/2").json()["overall"] == "live"
    assert client.get(f"{API}/drafts/{draft_id}").status_code == 404


def test_structural_edit_is_classified_structural(client: TestClient) -> None:
    draft_id = _draft(client)
    op = {"op": "delete", "collection": "connections", "key": "chw:Chiller/R_CV1->Chiller/R_C1"}
    client.post(f"{API}/drafts/{draft_id}/operations", json=[op])
    assert client.get(f"{API}/drafts/{draft_id}/diff").json()["overall"] == "structural"


def test_invalid_draft_cannot_be_applied(client: TestClient) -> None:
    draft_id = _draft(client)
    op = {"op": "delete", "collection": "assets", "key": "Chiller/R_C1"}
    client.post(f"{API}/drafts/{draft_id}/operations", json=[op])
    validation = client.get(f"{API}/drafts/{draft_id}/validation").json()
    assert validation["valid"] is False
    assert any("dangling" in i["message"] for i in validation["issues"])
    response = client.post(f"{API}/drafts/{draft_id}/apply", json={"message": "x"})
    assert response.status_code == 422
    assert client.get(f"{API}/summary").json()["revision"] == 1


def test_operation_that_does_not_apply_is_400(client: TestClient) -> None:
    draft_id = _draft(client)
    op = {"op": "delete", "collection": "assets", "key": "Nope"}
    assert client.post(f"{API}/drafts/{draft_id}/operations", json=[op]).status_code == 400


def test_stale_draft_conflicts(client: TestClient) -> None:
    first, second = _draft(client), _draft(client)
    conditions = {"op": "set_conditions", "value": {"utility_available": False}}
    for draft_id in (first, second):
        client.post(f"{API}/drafts/{draft_id}/operations", json=[conditions])
    assert client.post(f"{API}/drafts/{first}/apply", json={"message": "a"}).status_code == 201
    assert client.post(f"{API}/drafts/{second}/apply", json={"message": "b"}).status_code == 409


def test_discard_draft(client: TestClient) -> None:
    draft_id = _draft(client)
    assert client.delete(f"{API}/drafts/{draft_id}").status_code == 204
    assert client.get(f"{API}/drafts").json() == []

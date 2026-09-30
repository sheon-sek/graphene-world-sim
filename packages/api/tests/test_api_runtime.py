from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gws_api.app import create_app
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.store import SqliteStore

ROOT = Path(__file__).resolve().parents[3]
API = "/api/runtime"
IT = "~IT-DH01"


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    store = SqliteStore()
    store.create_revision(build(Sources.read(ROOT / "data" / "graphene")), "import", "importer")
    with TestClient(create_app(store)) as client:
        yield client
    store.close()


def _session(client: TestClient) -> str:
    response = client.post(f"{API}/sessions", json={"scope": [IT, "UPS/UPS 1"], "dt": 1.0})
    assert response.status_code == 201, response.text
    sid: str = response.json()["id"]
    return sid


def test_session_steps_takes_faults_and_replays(client: TestClient) -> None:
    sid = _session(client)
    assert client.get(f"{API}/sessions/{sid}").json()["revision"] == 1
    frame = client.post(f"{API}/sessions/{sid}/step", json={"steps": 2}).json()
    assert frame["step"] == 2 and frame["state"][IT]["P"] > 0

    fault = client.post(
        f"{API}/sessions/{sid}/faults", json={"target": IT, "mode": "emergency_power_off"}
    ).json()
    frame = client.post(f"{API}/sessions/{sid}/step").json()
    assert frame["state"][IT]["P"] == 0
    cleared = client.delete(f"{API}/sessions/{sid}/faults/{fault['id']}").json()
    assert cleared["cleared_at"] is not None  # latched until reset
    assert client.post(f"{API}/sessions/{sid}/reset", json={"target": IT}).json() == {
        "cleared": [fault["id"]]
    }
    conditions = client.put(f"{API}/sessions/{sid}/conditions", json={"utility_available": False})
    assert conditions.json()["utility_available"] is False
    final = client.post(f"{API}/sessions/{sid}/step", json={"steps": 5}).json()

    kinds = [e["kind"] for e in client.get(f"{API}/sessions/{sid}/events").json()]
    assert kinds == ["init", "fault", "clear", "reset", "conditions"]
    copy = client.post(f"{API}/sessions/{sid}/replay").json()
    assert client.get(f"{API}/sessions/{copy['id']}/frame").json() == final


def test_bad_inputs_are_422_and_unknown_sessions_404(client: TestClient) -> None:
    sid = _session(client)
    bad = client.post(f"{API}/sessions/{sid}/faults", json={"target": IT, "mode": "melt"})
    assert bad.status_code == 422
    assert client.post(f"{API}/sessions", json={"scope": ["Nope"]}).status_code == 422
    assert client.get(f"{API}/sessions/R999").status_code == 404


def test_snapshot_restore(client: TestClient) -> None:
    sid = _session(client)
    snap = client.post(f"{API}/sessions/{sid}/snapshots", json={"label": "start"}).json()
    ahead = client.post(f"{API}/sessions/{sid}/step", json={"steps": 3}).json()
    back = client.post(f"{API}/sessions/{sid}/snapshots/{snap['id']}/restore").json()
    assert back["step"] == 0
    assert client.post(f"{API}/sessions/{sid}/step", json={"steps": 3}).json() == ahead


def test_stream_pushes_frames_while_running(client: TestClient) -> None:
    sid = _session(client)
    with client.websocket_connect(f"{API}/sessions/{sid}/stream") as ws:
        assert ws.receive_json()["step"] == 0
        client.post(f"{API}/sessions/{sid}/run", json={"speed": 0})
        steps = [ws.receive_json()["step"] for _ in range(3)]
        client.post(f"{API}/sessions/{sid}/pause")
    assert steps == sorted(steps) and steps[0] >= 1
    assert client.get(f"{API}/sessions/{sid}").json()["running"] is False
    assert client.delete(f"{API}/sessions/{sid}").status_code == 204

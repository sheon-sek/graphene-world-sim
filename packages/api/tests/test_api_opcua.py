"""The serving process's OPC UA surface: runtime sessions seen through a real OPC UA client."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from asyncua import Client, ua
from fastapi.testclient import TestClient

from gws_api.app import create_app
from gws_opcua.nodeid import point_node_id
from gws_world_model import ops
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.store import SqliteStore

ROOT = Path(__file__).resolve().parents[3]
API = "/api"
SCOPE = ["~IT-DH01", "UPS/UPS 1", "Genset/Genset 1"]
IT_LOAD = "Environment Monitoring/Level 1/DH01/IT Load"
MODE = "Genset/Genset 1/Auto_Manual"
CHILLER = "Chiller/R_C1/Input Power"
NEW_POINT = "Demo Panel/DP-1/Temperature"


def _endpoint() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/graphene/twin"


@pytest.fixture(scope="module")
def served() -> Iterator[tuple[TestClient, str, SqliteStore]]:
    store = SqliteStore()
    doc = build(Sources.read(ROOT / "data" / "graphene"))
    store.create_revision(doc, "import", "importer")
    grown = ops.apply(
        doc,
        [
            ops.Put(
                collection="point_bindings",
                value={
                    "path": NEW_POINT,
                    "data_type": "Float4",
                    "unit": "degC",
                    "point_class": "process_value",
                    "source": {"kind": "static", "value": 21.5},
                },
            )
        ],
    )
    store.create_revision(grown, "add a panel", "test")
    endpoint = _endpoint()
    with TestClient(create_app(store, opcua_endpoint=endpoint)) as client:
        yield client, endpoint, store
    store.close()


def _node(client: Client, path: str) -> Any:
    return client.get_node(ua.NodeId(point_node_id(path), 2))


async def _read(client: Client, path: str) -> ua.DataValue:
    value: ua.DataValue = await _node(client, path).read_data_value(raise_on_bad_status=False)
    return value


async def _eventually(check: Any, seconds: float = 5.0) -> None:
    for _ in range(int(seconds / 0.1)):
        if await check():
            return
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached")


def test_a_session_is_served_with_timestamps_scope_and_commands(
    served: tuple[TestClient, str, SqliteStore],
) -> None:
    http, endpoint, _ = served
    status = http.get(f"{API}/opcua").json()
    assert status["session"] is None and status["revision"] == 2 and status["points"] == 8812
    sid = http.post(f"{API}/runtime/sessions", json={"scope": SCOPE, "revision": 1}).json()["id"]

    async def main() -> None:
        async with Client(endpoint) as client:
            idle = await _read(client, IT_LOAD)
            assert idle.StatusCode.value == ua.StatusCodes.BadOutOfService

            attached = http.put(f"{API}/opcua/session", json={"session": sid}).json()
            assert attached["session"] == sid and attached["points"] == 8811
            frame = http.post(f"{API}/runtime/sessions/{sid}/step", json={"steps": 3}).json()

            expected = frame["points"][IT_LOAD]["value"]

            async def published() -> bool:
                # Attaching publishes the t=0 frame first; wait for the stepped one.
                value = await _read(client, IT_LOAD)
                return bool(
                    value.StatusCode.is_good()
                    and value.Value.Value == pytest.approx(expected, rel=1e-5)
                )

            await _eventually(published)
            load = await _read(client, IT_LOAD)
            now = datetime.now(UTC)
            assert load.SourceTimestamp is not None
            assert abs(load.SourceTimestamp - now) < timedelta(seconds=30)
            outside = await _read(client, CHILLER)
            assert outside.StatusCode.value == ua.StatusCodes.BadOutOfService
            with pytest.raises(ua.uaerrors.BadNodeIdUnknown):
                await _node(client, NEW_POINT).read_browse_name()

            # A write to a command point is a runtime command in the event log.
            await _node(client, MODE).write_value(ua.DataValue(ua.Variant(0, ua.VariantType.Int32)))
            with pytest.raises(ua.uaerrors.BadNotWritable):
                await _node(client, IT_LOAD).write_value(
                    ua.DataValue(ua.Variant(1.0, ua.VariantType.Float))
                )
            with pytest.raises(ua.uaerrors.BadOutOfRange):
                await _node(client, "Chiller/R_C1/Auto_Manual").write_value(
                    ua.DataValue(ua.Variant(0, ua.VariantType.Int32))
                )
            events = http.get(f"{API}/runtime/sessions/{sid}/events").json()
            assert events[-1]["kind"] == "command"
            assert events[-1]["payload"] == {"target": MODE, "signal": None, "value": 0}
            frame = http.post(f"{API}/runtime/sessions/{sid}/step").json()
            assert frame["points"][MODE]["value"] == 0

            # The genset reports through gateway B: losing it is a communication failure.
            http.post(
                f"{API}/runtime/sessions/{sid}/faults",
                json={"target": "Network Topology/GATEWAY B", "mode": "failure"},
            )
            http.post(f"{API}/runtime/sessions/{sid}/step")

            async def comm_lost() -> bool:
                code = (await _read(client, MODE)).StatusCode.value
                return bool(code == ua.StatusCodes.BadCommunicationError)

            await _eventually(comm_lost)

            # A structural edit reaches the connected client without reconnecting.
            http.post(f"{API}/runtime/sessions/{sid}/reinit", json={"revision": 2})

            async def grown() -> bool:
                try:
                    await _node(client, NEW_POINT).read_browse_name()
                except ua.uaerrors.BadNodeIdUnknown:
                    return False
                return True

            await _eventually(grown)
            assert http.get(f"{API}/opcua").json()["points"] == 8812

            http.delete(f"{API}/runtime/sessions/{sid}")
            assert http.get(f"{API}/opcua").json()["session"] is None
            with pytest.raises(ua.uaerrors.BadOutOfRange):
                await _node(client, MODE).write_value(
                    ua.DataValue(ua.Variant(1, ua.VariantType.Int32))
                )

    asyncio.run(main())


def test_attaching_an_unknown_session_is_404(served: tuple[TestClient, str, SqliteStore]) -> None:
    http, _, _ = served
    assert http.put(f"{API}/opcua/session", json={"session": "R999"}).status_code == 404


def test_without_an_endpoint_there_is_no_opcua_surface() -> None:
    with TestClient(create_app(SqliteStore())) as client:
        assert client.get(f"{API}/opcua").status_code == 404


def test_a_removed_asset_reads_bad_then_its_nodes_go(
    served: tuple[TestClient, str, SqliteStore],
) -> None:
    http, endpoint, store = served
    removed = "UPS/UPS 1/Frequency"
    doc = store.get(1)
    assert doc is not None
    revision = store.create_revision(ops.apply(doc, [ops.Remove(key="UPS/UPS 1")]), "remove", "t")
    sid = http.post(f"{API}/runtime/sessions", json={"scope": SCOPE, "revision": 1}).json()["id"]
    bridge = http.app.state.opcua  # type: ignore[attr-defined]
    bridge.removal_grace = 1.0

    async def main() -> None:
        async with Client(endpoint) as client:
            http.put(f"{API}/opcua/session", json={"session": sid})
            http.post(f"{API}/runtime/sessions/{sid}/step", json={"steps": 2})

            async def good() -> bool:
                return bool((await _read(client, removed)).StatusCode.is_good())

            await _eventually(good)
            swap = http.post(
                f"{API}/runtime/sessions/{sid}/swap", json={"revision": revision.number}
            )
            assert swap.status_code == 202 and swap.json()["removed"] == ["UPS/UPS 1"]

            async def bad() -> bool:
                code = (await _read(client, removed)).StatusCode.value
                return bool(code == ua.StatusCodes.BadNotFound)

            await _eventually(bad)
            assert http.get(f"{API}/runtime/sessions/{sid}/swap").json()["state"] == "applied"
            assert (await _read(client, IT_LOAD)).StatusCode.is_good()

            async def gone() -> bool:
                code = (await _read(client, removed)).StatusCode.value
                return bool(code == ua.StatusCodes.BadNodeIdUnknown)

            await _eventually(gone)
            http.delete(f"{API}/runtime/sessions/{sid}")

    asyncio.run(main())

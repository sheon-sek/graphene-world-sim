"""The OPC UA server, driven through a real asyncua client."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from asyncua import Client, ua

from gws_opcua.points import PointSpec, PointValue, Scalar
from gws_opcua.server import PointServer

T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
SPECS = [
    PointSpec("Chiller/R_C1/On_Off", "Int4", writable=True),
    PointSpec("Chiller/R_C1/Input Power", "Float4", unit="kW"),
    PointSpec("Genset/Genset 1/AC Voltage: L1-N", "Float8"),
    PointSpec("Dashboard/Load 50%", "Boolean"),
    PointSpec("Dashboard/Rack Map", "DataSet"),
]


def _endpoint() -> str:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/graphene/twin"


Scenario = Callable[[PointServer, Client], Awaitable[None]]


def _run(scenario: Scenario, on_write: Any = None) -> None:
    async def main() -> None:
        server = PointServer(_endpoint(), on_write=on_write, clock=lambda: T0)
        await server.set_points(SPECS)
        async with server, Client(server.endpoint) as client:
            await scenario(server, client)

    asyncio.run(main())


def _node(client: Client, path: str) -> Any:
    from gws_opcua.nodeid import point_node_id

    return client.get_node(ua.NodeId(point_node_id(path), 2))


def test_address_space_mirrors_export_paths_with_the_contract_node_ids() -> None:
    async def scenario(server: PointServer, client: Client) -> None:
        assert await client.get_namespace_index("urn:eetarp:graphene:demo:twin") == 2
        voltage = client.get_node("ns=2;s=point:Genset/Genset 1/AC Voltage%3A L1-N")
        assert (await voltage.read_browse_name()).Name == "AC Voltage: L1-N"
        assert await voltage.read_data_type_as_variant_type() == ua.VariantType.Double
        folder = client.get_node("ns=2;s=folder:Genset/Genset 1")
        assert (await folder.read_browse_name()).Name == "Genset 1"
        assert voltage in await folder.get_children()
        top = {(await n.read_browse_name()).Name for n in await client.nodes.objects.get_children()}
        assert {"Chiller", "Genset", "Dashboard"} <= top
        types = {
            p: await _node(client, p).read_data_type_as_variant_type()
            for p in ("Chiller/R_C1/On_Off", "Dashboard/Load 50%", "Dashboard/Rack Map")
        }
        assert types == {
            "Chiller/R_C1/On_Off": ua.VariantType.Int32,
            "Dashboard/Load 50%": ua.VariantType.Boolean,
            "Dashboard/Rack Map": ua.VariantType.String,
        }
        assert client.get_node("ns=2;s=point:Dashboard/Load 50%25") == _node(
            client, "Dashboard/Load 50%"
        )

    _run(scenario)


def test_values_carry_quality_and_the_simulation_timestamp() -> None:
    async def scenario(server: PointServer, client: Client) -> None:
        await server.publish(
            {
                "Chiller/R_C1/Input Power": PointValue(130.4, "good", T0),
                "Genset/Genset 1/AC Voltage: L1-N": PointValue(231.0, "bad", T0, "comm_lost"),
                "Dashboard/Rack Map": PointValue({"rows": [1, 2]}, "uncertain", T0, "out_of_range"),
                "Dashboard/Load 50%": PointValue(None, "good", T0),
            }
        )
        power = await _node(client, "Chiller/R_C1/Input Power").read_data_value()
        assert power.Value.Value == pytest.approx(130.4, rel=1e-6)
        assert power.StatusCode.is_good() and power.SourceTimestamp == T0
        voltage = await _node(client, "Genset/Genset 1/AC Voltage: L1-N").read_data_value(
            raise_on_bad_status=False
        )
        assert voltage.StatusCode.value == ua.StatusCodes.BadCommunicationError
        rack = await _node(client, "Dashboard/Rack Map").read_data_value(raise_on_bad_status=False)
        assert rack.StatusCode.value == ua.StatusCodes.UncertainEngineeringUnitsExceeded
        assert rack.Value.Value == '{"rows": [1, 2]}'
        empty = await _node(client, "Dashboard/Load 50%").read_data_value(raise_on_bad_status=False)
        assert empty.StatusCode.value == ua.StatusCodes.BadWaitingForInitialData

    _run(scenario)


def test_points_are_published_by_exception() -> None:
    later = T0 + timedelta(seconds=1)

    async def scenario(server: PointServer, client: Client) -> None:
        power, voltage = "Chiller/R_C1/Input Power", "Genset/Genset 1/AC Voltage: L1-N"
        await server.publish(
            {power: PointValue(130.0, "good", T0), voltage: PointValue(230.0, "good", T0)}
        )
        written = await server.publish(
            {power: PointValue(130.0, "good", later), voltage: PointValue(231.0, "good", later)}
        )
        assert written == 1
        unchanged = await _node(client, power).read_data_value()
        changed = await _node(client, voltage).read_data_value()
        assert unchanged.SourceTimestamp == T0 and changed.SourceTimestamp == later
        assert await server.publish({power: PointValue(130.0, "bad", later, "comm_lost")}) == 1

    _run(scenario)


def test_only_command_points_accept_writes_and_the_handler_decides() -> None:
    writes: list[tuple[str, Scalar]] = []

    async def on_write(path: str, value: Scalar) -> str | None:
        writes.append((path, value))
        return None if value in (0, 1) else "On_Off takes 0 or 1"

    async def scenario(server: PointServer, client: Client) -> None:
        on_off = _node(client, "Chiller/R_C1/On_Off")
        await on_off.write_value(ua.DataValue(ua.Variant(0, ua.VariantType.Int32)))
        echoed = await on_off.read_data_value()
        assert echoed.Value.Value == 0 and echoed.SourceTimestamp == T0
        with pytest.raises(ua.uaerrors.BadOutOfRange):
            await on_off.write_value(ua.DataValue(ua.Variant(7, ua.VariantType.Int32)))
        with pytest.raises(ua.uaerrors.BadTypeMismatch):
            await on_off.write_value(ua.DataValue(ua.Variant("on", ua.VariantType.String)))
        with pytest.raises(ua.uaerrors.BadNotWritable):
            await _node(client, "Chiller/R_C1/Input Power").write_value(
                ua.DataValue(ua.Variant(1.0, ua.VariantType.Float))
            )
        assert writes == [("Chiller/R_C1/On_Off", 0), ("Chiller/R_C1/On_Off", 7)]

    _run(scenario, on_write=on_write)


def test_new_points_appear_for_a_connected_client_with_a_model_change_event() -> None:
    class Events:
        def __init__(self) -> None:
            self.received: list[Any] = []

        def event_notification(self, event: Any) -> None:
            self.received.append(event)

    async def scenario(server: PointServer, client: Client) -> None:
        events = Events()
        subscription = await client.create_subscription(50, events)
        await subscription.subscribe_events(
            client.nodes.server, ua.ObjectIds.GeneralModelChangeEventType
        )
        change = await server.set_points(
            [s for s in SPECS if not s.path.startswith("Dashboard/")]
            + [PointSpec("New Asset/NA-1/Temperature", "Float4")]
        )
        assert change.added == ("New Asset/NA-1/Temperature",)
        assert set(change.removed) == {"Dashboard/Load 50%", "Dashboard/Rack Map"}
        new = _node(client, "New Asset/NA-1/Temperature")
        assert (await new.read_browse_name()).Name == "Temperature"
        top = {(await n.read_browse_name()).Name for n in await client.nodes.objects.get_children()}
        assert "New Asset" in top and "Dashboard" not in top
        for _ in range(50):
            if events.received:
                break
            await asyncio.sleep(0.05)
        verbs = {(c.Affected.Identifier, c.Verb) for c in events.received[0].Changes}
        assert ("point:New Asset/NA-1/Temperature", 1) in verbs
        assert ("folder:Dashboard", 2) in verbs

    _run(scenario)


def test_rejects_a_path_that_is_both_a_point_and_a_folder() -> None:
    async def main() -> None:
        server = PointServer(_endpoint())
        async with server:
            with pytest.raises(ValueError, match="both a point and a folder"):
                await server.set_points([PointSpec("A/B", "Int4"), PointSpec("A/B/C", "Int4")])

    asyncio.run(main())

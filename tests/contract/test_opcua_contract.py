"""OPC UA compatibility contract (ADR-0003).

The existing Ignition `[DemoTwin]` provider addresses points by these NodeIds, so they must not
change. The last test serves the whole Graphene World Model and compares the address space a
client browses with the Asset Model: every exported point, at its path, with its data type.
"""

import asyncio
import socket
from pathlib import Path
from typing import Any

import pytest
from asyncua import Client, ua

from gws_api.opcua import point_specs
from gws_opcua.nodeid import (
    NAMESPACE_URI,
    export_path_of,
    folder_node_id,
    point_node_id,
)
from gws_opcua.server import VARIANT_TYPES, PointServer
from gws_world_model.importers.graphene import Sources, build

ROOT = Path(__file__).resolve().parents[2]


def test_namespace_uri_is_the_one_ignition_binds_to() -> None:
    assert NAMESPACE_URI == "urn:eetarp:graphene:demo:twin"


@pytest.mark.parametrize(
    ("export_path", "node_id"),
    [
        ("Chiller/R_C1/On_Off", "point:Chiller/R_C1/On_Off"),
        ("Genset/Genset 1/AC Voltage: L1-N", "point:Genset/Genset 1/AC Voltage%3A L1-N"),
        ("Dashboard/Load 50%", "point:Dashboard/Load 50%25"),
        ("Odd/100%:x", "point:Odd/100%25%3Ax"),
    ],
)
def test_point_node_ids_encode_percent_then_colon(export_path: str, node_id: str) -> None:
    assert point_node_id(export_path) == node_id
    assert export_path_of(node_id) == export_path


def test_literal_percent_3a_in_a_path_round_trips() -> None:
    path = "Odd/literal %3A text"
    assert export_path_of(point_node_id(path)) == path


def test_folder_node_ids_keep_the_path_unchanged() -> None:
    assert folder_node_id("Genset/Genset 1") == "folder:Genset/Genset 1"


def test_rejects_non_point_node_ids() -> None:
    with pytest.raises(ValueError):
        export_path_of("folder:Genset")


async def _browse(client: Client) -> dict[str, tuple[str, ua.VariantType]]:
    """Every variable in the simulator's namespace: NodeId -> (browse path, data type)."""
    found: dict[str, tuple[str, ua.VariantType]] = {}
    pending: list[tuple[Any, str]] = [(client.nodes.objects, "")]
    while pending:
        node, prefix = pending.pop()
        for child in await node.get_children():
            if child.nodeid.NamespaceIndex != 2:
                continue
            name = (await child.read_browse_name()).Name
            path = f"{prefix}/{name}" if prefix else name
            if await child.read_node_class() == ua.NodeClass.Variable:
                found[child.nodeid.Identifier] = (
                    path,
                    await child.read_data_type_as_variant_type(),
                )
            else:
                assert child.nodeid.Identifier == folder_node_id(path)
                pending.append((child, path))
    return found


def test_address_space_is_the_asset_model() -> None:
    sources = Sources.read(ROOT / "data" / "graphene")
    expected = {
        point_node_id(p.path): (p.path, VARIANT_TYPES[p.data_type])
        for p in sources.export.points.values()
    }
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        endpoint = f"opc.tcp://127.0.0.1:{s.getsockname()[1]}/graphene/twin"

    async def main() -> dict[str, tuple[str, ua.VariantType]]:
        server = PointServer(endpoint)
        await server.set_points(point_specs(build(sources)))
        async with server, Client(endpoint, timeout=30) as client:
            assert await client.get_namespace_index(NAMESPACE_URI) == 2
            return await _browse(client)

    browsed = asyncio.run(main())
    assert len(expected) == 8811
    assert browsed == expected

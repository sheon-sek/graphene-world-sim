"""OPC UA compatibility contract (ADR-0003).

The existing Ignition `[DemoTwin]` provider addresses points by these NodeIds, so they must not
change. The full address-space comparison against the Asset Model arrives with the OPC UA
server (#45).
"""

import pytest

from gws_opcua.nodeid import (
    NAMESPACE_URI,
    export_path_of,
    folder_node_id,
    point_node_id,
)


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

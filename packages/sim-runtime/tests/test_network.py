from __future__ import annotations

from gws_runtime.network import COMM_DISCONNECTED, LINK_DOWN, ControlNetwork
from gws_world_model.model import (
    Asset,
    AssetCategory,
    ComponentType,
    Connection,
    Domain,
    Endpoint,
    ParameterSpec,
    PortSpec,
    Scalar,
    Site,
    WorldModel,
)

NET_DEVICE = ComponentType(
    id="Net Device",
    name="Net device",
    category=AssetCategory.DEVICE,
    parameters={
        "role": ParameterSpec(default="device"),
        "serves": ParameterSpec(default=""),
    },
    ports={"net": PortSpec(domain=Domain.NET)},
)
SWITCH_VIEW = ComponentType(
    id="Switch View",
    name="Switch port view",
    category=AssetCategory.DEVICE,
    parameters={"device": ParameterSpec(default=""), "port_count": ParameterSpec(default=4)},
    ports={"net": PortSpec(domain=Domain.NET)},
)
EQUIPMENT = ComponentType(id="Pump", name="Pump", category=AssetCategory.EQUIPMENT)


def _link(a: str, b: str) -> Connection:
    return Connection(
        id=f"net:{a}->{b}",
        domain=Domain.NET,
        source=Endpoint(node=a, port="net"),
        target=Endpoint(node=b, port="net"),
    )


def network_world() -> WorldModel:
    """GW -- SW1 -- SW3 -- D1, with D2 dual-homed on SW1 and SW2 (both under GW). V is the
    port view of SW3. P1 (Cooling) and P2 (Support) have no net port."""
    devices: dict[str, dict[str, Scalar]] = {
        "GW": {"role": "gateway", "serves": "Cooling, Airside"},
        "SW1": {},
        "SW2": {},
        "SW3": {},
        "D1": {},
        "D2": {},
    }
    assets = {
        name: Asset(id=name, type="Net Device", name=name, parameters=params, system="Network")
        for name, params in devices.items()
    }
    assets["V"] = Asset(id="V", type="Switch View", name="V", parameters={"device": "SW3"})
    assets["P1"] = Asset(id="P1", type="Pump", name="P1", system="Cooling")
    assets["P2"] = Asset(id="P2", type="Pump", name="P2", system="Support")
    links = [
        _link(*pair)
        for pair in (
            ("GW", "SW1"),
            ("GW", "SW2"),
            ("SW1", "SW3"),
            ("SW3", "D1"),
            ("SW1", "D2"),
            ("SW2", "D2"),
        )
    ]
    return WorldModel(
        site=Site(id="t", name="t"),
        component_types={t.id: t for t in (NET_DEVICE, SWITCH_VIEW, EQUIPMENT)},
        assets=assets,
        connections={c.id: c for c in links},
    )


def unreachable(net: ControlNetwork) -> set[str]:
    return {
        a for a in ("GW", "SW1", "SW2", "SW3", "D1", "D2", "V", "P1", "P2") if not net.reachable(a)
    }


def test_gateways_and_attachments_come_from_data() -> None:
    net = ControlNetwork.from_world(network_world())
    assert net.gateways == ("GW",)
    assert net.attached == {"P1": ("GW",)}
    assert unreachable(net) == set()
    assert net.path("D1") == ("SW3", "SW1", "GW")
    assert net.path("P1") == ("GW",)
    assert net.path("P2") == ()  # not on the modelled network: reachable by default


def test_switch_failure_cuts_off_everything_behind_it_and_restore_brings_it_back() -> None:
    net = ControlNetwork.from_world(network_world())
    net.fail("SW1")
    net.step(1.0)
    assert unreachable(net) == {"SW1", "SW3", "D1", "V"}
    assert net.path("D2") == ("SW2", "GW")  # the redundant path keeps it reachable
    signals = net.signals()
    assert signals["D1"]["Comm"] == COMM_DISCONNECTED
    assert signals["SW1"]["up"] is False
    net.restore("SW1")
    net.step(2.0)
    assert unreachable(net) == set()


def test_link_failure_cuts_off_only_what_has_no_other_path() -> None:
    net = ControlNetwork.from_world(network_world())
    net.fail("net:SW3->D1")
    net.fail("net:SW1->D2")
    net.step(1.0)
    assert unreachable(net) == {"D1"}
    assert net.path("D2") == ("SW2", "GW")


def test_reachability_changes_only_at_the_next_step() -> None:
    net = ControlNetwork.from_world(network_world())
    net.fail("GW")
    assert net.reachable("P1")
    net.step(1.0)
    assert not net.reachable("P1")
    assert not net.reachable("D2")
    assert net.reachable("P2")


def test_a_port_view_shares_its_device_state() -> None:
    net = ControlNetwork.from_world(network_world())
    net.fail("V")
    net.step(1.0)
    assert not net.reachable("SW3") and not net.reachable("D1")
    view = net.signals()["V"]
    assert (view["Ports Down"], view["Ports Up"], view["Port Count"]) == (4, 0, 4)
    net.restore("SW3")
    net.fail("V/Ports/Port 02")
    net.step(2.0)
    view = net.signals()["V"]
    assert view["Ports/Port 02/Link Status"] == LINK_DOWN
    assert (view["Ports Down"], view["Ports Up"]) == (1, 3)
    assert net.reachable("D1")


def test_snapshot_restores_failures() -> None:
    net = ControlNetwork.from_world(network_world())
    net.fail("SW1")
    net.fail("net:GW->SW2")
    net.step(1.0)
    state = net.snapshot()
    other = ControlNetwork.from_world(network_world())
    other.restore_state(state)
    assert unreachable(other) == unreachable(net)
    assert other.failed() == ("SW1", "net:GW->SW2")

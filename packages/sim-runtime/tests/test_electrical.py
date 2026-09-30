from __future__ import annotations

import json
from pathlib import Path

import pytest

import gws_runtime.controllers.electrical  # noqa: F401  (registers the blocks)
from gws_runtime import controllers
from gws_runtime.controllers import Block
from gws_runtime.electrical import ElectricalNetwork
from gws_runtime.master import electrical_points
from gws_runtime.values import Value, split_ref
from gws_world_model import library
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import (
    Asset,
    Connection,
    ControlBinding,
    Domain,
    Endpoint,
    Location,
    Scalar,
    Site,
    WorldModel,
)

ROOT = Path(__file__).resolve().parents[3]


class Bus:
    """The controllers' view: the network's true state, commands through `command`."""

    def __init__(self, net: ElectricalNetwork) -> None:
        self.net = net
        self.state = net.signals()

    def read(self, reference: str) -> Value:
        asset, signal = split_ref(reference)
        return self.state[asset][signal]

    def write(self, reference: str, value: Value) -> None:
        asset, signal = split_ref(reference)
        assert isinstance(value, bool | int | float)
        self.net.command(asset, signal, value)


class Plant:
    """Steps a network and its electrical control bindings like the master does."""

    def __init__(self, doc: WorldModel, net: ElectricalNetwork | None = None) -> None:
        self.doc = doc
        self.net = net or ElectricalNetwork.from_world(doc)
        self.blocks: list[Block] = controllers.build(
            doc, lambda b: b.function in ("ats", "genset_start", "load_shed")
        )[0]
        self.t = 0.0

    def step(self, n: int = 1) -> dict[str, dict[str, float | bool]]:
        for _ in range(n):
            self.t += 1.0
            self.net.step(self.t, 1.0)
            bus = Bus(self.net)
            for b in self.blocks:
                b.step(self.t, 1.0, bus)
        return self.net.signals()


def _asset(id: str, type: str, **parameters: Scalar) -> Asset:
    return Asset(id=id, type=type, name=id, parameters=parameters, location=Location())


def _power(src: str, dst: str, sport: str = "power_out", dport: str = "power_in") -> Connection:
    return Connection(
        id=f"power:{src}->{dst}",
        domain=Domain.POWER,
        source=Endpoint(node=src, port=sport),
        target=Endpoint(node=dst, port=dport),
    )


def small_world(**ups: Scalar) -> WorldModel:
    """Utility and one genset through an ATS onto an MSB, which feeds a plain load through a
    feeder and two UPS whose branch circuits share one IT load."""
    types = library.load()
    assets = [
        _asset("TX", "Utility Supply", s_rated=1000.0),
        _asset("INC", "GPM96"),
        _asset("G1", "Genset", p_prime=1000.0, s_rated=1250.0),
        _asset("ATS", "ATS"),
        _asset("MSB", "GPM96"),
        _asset("F1", "GPM96"),
        _asset("PUMP", "Chiller Pump"),
        _asset("UPS1", "UPS", s_rated=200.0, **ups),
        _asset("UPS2", "UPS", s_rated=200.0, **ups),
        _asset("B1", "BCPM"),
        _asset("B2", "BCPM"),
        _asset("IT", "IT Load"),
    ]
    connections = [
        _power("TX", "INC"),
        _power("INC", "ATS", dport="normal_in"),
        _power("G1", "ATS", dport="emergency_in"),
        _power("ATS", "MSB"),
        _power("MSB", "F1"),
        _power("F1", "PUMP"),
        _power("MSB", "UPS1"),
        _power("MSB", "UPS2"),
        _power("UPS1", "B1"),
        _power("UPS2", "B2"),
        _power("B1", "IT"),
        _power("B2", "IT"),
    ]
    bindings = [
        ControlBinding(
            id="ATS/ats",
            controller="ATS",
            function="ats",
            reads=("ATS:V_normal_pu", "ATS:V_emergency_pu"),
            drives=("ATS:position",),
            parameters={"retransfer_delay": 30.0},
        ),
        ControlBinding(
            id="ATS/genset_start",
            controller="ATS",
            function="genset_start",
            reads=("ATS:V_normal_pu", "ATS:source"),
            drives=("G1:start",),
            parameters={"cooldown": 20.0},
        ),
    ]
    return WorldModel(
        site=Site(id="t", name="test"),
        component_types={t.id: types[t.id] for t in types.values()},
        assets={a.id: a for a in assets},
        connections={c.id: c for c in connections},
        control_bindings={b.id: b for b in bindings},
    )


def small_plant(**ups: Scalar) -> Plant:
    plant = Plant(small_world(**ups))
    plant.net.set_demand("IT", 200e3)
    plant.net.set_demand("PUMP", 50e3)
    return plant


def test_utility_loss_transfers_to_genset_and_ups_rides_through() -> None:
    plant = small_plant()
    s = plant.step(3)
    assert s["IT"]["energised"] and s["PUMP"]["energised"]
    assert s["UPS1"]["P"] == pytest.approx(100e3, rel=0.02)  # the IT load shares its feeds
    assert s["TX"]["P"] > 250e3 and s["G1"]["P"] == 0.0

    plant.net.set_utility(False)
    lost = plant.t + 1
    s = plant.step()
    assert s["UPS1"]["on_battery"] and s["IT"]["energised"]
    assert not s["PUMP"]["energised"] and s["UPS1"]["P_in"] == 0.0
    soc = s["UPS1"]["soc"]
    while not plant.net.signals()["MSB"]["energised"]:
        assert plant.t - lost < 15.0, "no transfer within 15 s"
        s = plant.step()
        assert s["IT"]["energised"]
        assert s["UPS1"]["soc"] <= soc
        soc = s["UPS1"]["soc"]
    assert plant.t - lost <= 15.0
    assert soc < 1.0

    s = plant.step()
    assert s["ATS"]["source"] == 2.0 and s["G1"]["running"]
    assert s["PUMP"]["energised"] and not s["UPS1"]["on_battery"]
    assert s["G1"]["P"] == pytest.approx(s["ATS"]["P"], rel=0.01)
    charge = s["UPS1"]["P_charge"]
    assert charge > 0.0
    assert s["UPS1"]["P_in"] == pytest.approx((s["UPS1"]["P"] + charge) / 0.96, rel=1e-6)
    s2 = plant.step(5)
    assert s2["UPS1"]["soc"] > s["UPS1"]["soc"]

    plant.net.set_utility(True)
    back = plant.t + 1
    while plant.net.signals()["ATS"]["source"] != 1.0:
        s = plant.step()
        assert plant.t - back < 40.0
    assert plant.t - back >= 30.0  # retransfer delay
    s = plant.step()
    assert s["TX"]["P"] > 0.0 and s["G1"]["P"] == 0.0
    s = plant.step(25)
    assert not s["G1"]["running"]  # stopped after its cooldown


def test_breaker_trip_de_energises_downstream_until_reset() -> None:
    plant = small_plant()
    plant.step(2)
    plant.net.fault("F1", "breaker_trip", {})
    s = plant.step()
    assert s["F1"]["tripped"] and not s["F1"]["energised"] and not s["PUMP"]["energised"]
    assert s["MSB"]["energised"] and s["IT"]["energised"]
    assert s["MSB"]["P"] < 260e3  # the pump's 50 kW is gone from the upstream meter

    plant.net.fault("MSB", "breaker_trip", {})
    s = plant.step()
    assert not s["UPS1"]["V_in_pu"] and s["UPS1"]["on_battery"] and s["IT"]["energised"]

    plant.net.clear("F1", "breaker_trip")
    plant.net.clear("MSB", "breaker_trip")
    s = plant.step()
    assert s["F1"]["tripped"] and not s["PUMP"]["energised"]  # latched until reset
    plant.net.command("MSB", "reset", True)
    plant.net.command("F1", "reset", True)
    s = plant.step()
    assert s["PUMP"]["energised"] and not s["UPS1"]["on_battery"]


def test_ups_battery_exhaustion_drops_its_output() -> None:
    plant = small_plant(battery_autonomy=20.0)
    plant.step(2)
    plant.net.fault("G1", "fail_to_start", {})
    plant.net.set_utility(False)
    s = plant.step(15)
    assert s["IT"]["energised"] and 0.0 < s["UPS1"]["soc"] < 1.0
    s = plant.step(30)
    assert s["G1"]["locked_out"] and s["UPS1"]["soc"] == 0.0
    assert not s["UPS1"]["inverter_on"] and not s["UPS1"]["energised"]
    assert not s["IT"]["energised"] and plant.net.supply("IT") == 0.0


def test_multi_feed_load_moves_to_the_remaining_feed() -> None:
    plant = small_plant()
    plant.step(2)
    plant.net.fault("UPS1", "output_fault", {})
    s = plant.step()
    assert s["UPS1"]["P"] == 0.0 and not s["B1"]["energised"]
    assert s["UPS2"]["P"] == pytest.approx(200e3, rel=0.02) and s["IT"]["energised"]


def test_scope_keeps_the_supply_path_only() -> None:
    net = ElectricalNetwork.from_world(small_world(), scope={"IT"})
    assert net.loads == {"IT"}
    assert {"UPS1", "MSB", "ATS", "TX", "G1"} <= net.assets and "F1" not in net.assets


def test_snapshot_restore_gives_identical_steps() -> None:
    plant = small_plant()
    plant.step(3)
    plant.net.set_utility(False)
    plant.step(8)  # mid-sequence: on battery, genset starting
    state = json.loads(json.dumps(plant.net.snapshot()))
    blocks = json.loads(json.dumps([b.snapshot() for b in plant.blocks]))

    other = Plant(plant.doc)
    other.net.restore(state)
    for b, s in zip(other.blocks, blocks, strict=True):
        b.restore(s)
    other.t = plant.t
    for _ in range(20):
        assert plant.step() == other.step()


@pytest.fixture(scope="module")
def site() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


def _design_demand(doc: WorldModel, load: str) -> float:
    """Design electrical demand in W: IT from the conditions, others from type ratings."""
    if load.startswith("room:"):
        return 5e3
    asset = doc.assets[load]
    if asset.type == "IT Load":
        it = doc.conditions.it_load[str(asset.location.room)]
        return it.design_kw * it.fraction * 1e3
    params = {k: s.default for k, s in doc.component_types[asset.type].parameters.items()}
    for name in ("rated_power", "fan_power_nominal", "pump_power"):
        if isinstance(value := params.get(name), float):
            return value * 1e3
    q, cop = params.get("q_nominal"), params.get("cop_nominal")
    if isinstance(q, float) and isinstance(cop, float):
        return q / cop * 1e3
    return 10e3


def test_site_rides_through_utility_loss_on_gensets(site: WorldModel) -> None:
    plant = Plant(site)
    it_loads = sorted(a for a in plant.net.loads if a.startswith("~IT-"))
    assert len(it_loads) == 8
    for load in plant.net.loads:
        plant.net.set_demand(load, _design_demand(site, load))
    s = plant.step()
    dead = [a for a in plant.net.loads if not s[a]["energised"]]
    assert dead == []
    assert s["~ATS-A"]["P"] + s["~ATS-B"]["P"] > 7e6

    plant.net.set_utility(False)
    s = plant.step()
    assert all(s[a]["energised"] for a in it_loads)
    assert not s["Chiller/R_C1"]["energised"]
    s = plant.step(14)
    assert s["~ATS-A"]["source"] == 2.0 and s["~ATS-B"]["source"] == 2.0
    assert all(s[a]["energised"] for a in it_loads)
    assert all(s[a]["energised"] for a in plant.net.loads)
    gensets = [f"Genset/Genset {n}" for n in range(1, 7)]
    assert all(s[g]["running"] and s[g]["P"] > 0 for g in gensets)
    assert sum(s[g]["P"] for g in gensets) == pytest.approx(
        s["~ATS-A"]["P"] + s["~ATS-B"]["P"], rel=0.01
    )


def test_meters_read_power_quality_from_the_supplying_island() -> None:
    plant = small_plant()
    s = plant.step(2)
    msb = s["MSB"]
    assert msb["V_ln"] == pytest.approx(msb["V_ll"] / 3**0.5)
    assert msb["P_ph"] == pytest.approx(msb["P"] / 3)
    assert msb["Hz"] == 50.0
    # Drives and IT power supplies draw harmonic current; the utility adds background THD(V).
    assert 0.0 < msb["THDA"] < 0.35 and s["F1"]["THDA"] == pytest.approx(0.35)
    assert msb["THDV"] >= 0.01 and s["F1"]["I_n"] == 0.0
    # The UPS rectifiers isolate the IT loads' triplen currents from the MSB.
    assert msb["I_n"] == 0.0 and s["B1"]["I_n"] > 0.0

    plant.net.set_utility(False)
    s = plant.step()
    assert s["UPS1"]["on_battery"] and s["UPS1"]["Hz"] == 50.0  # its own oscillator
    assert s["MSB"]["Hz"] == 0.0
    s = plant.step(20)
    load = s["G1"]["P"] / 1e6
    assert s["MSB"]["Hz"] == pytest.approx(50.0 * (1 - 0.03 * (load - 0.5)))
    assert s["UPS1"]["Hz"] == s["MSB"]["Hz"]  # synchronised to its input again


def test_genset_engine_warms_up_and_its_oil_protection_shuts_it_down() -> None:
    plant = small_plant()
    plant.step(2)
    plant.net.set_utility(False)
    s = plant.step(20)
    g = s["G1"]
    assert g["running"] and g["speed"] == pytest.approx(g["Hz"] / 2)
    assert g["p_oil"] > 380e3 and g["V_battery"] == 27.6 and g["run_s"] > 0.0
    cold = g["T_coolant"]
    s = plant.step(60)
    assert s["G1"]["T_coolant"] > cold

    plant.net.fault("G1", "low_oil_pressure", {"pressure_fraction": 0.6})
    s = plant.step()
    assert s["G1"]["running"] and s["G1"]["oil_prealarm"] and s["G1"]["prealarm"]
    plant.net.fault("G1", "low_oil_pressure", {"pressure_fraction": 0.3})
    s = plant.step()
    assert s["G1"]["locked_out"] and s["G1"]["oil_shutdown"] and s["G1"]["alarm"]
    assert not s["G1"]["running"] and s["G1"]["p_oil"] == 0.0
    plant.net.command("G1", "reset", True)
    assert plant.step()["G1"]["locked_out"]  # the cause is still there
    plant.net.clear("G1", "low_oil_pressure")
    plant.net.command("G1", "reset", True)
    assert not plant.step()["G1"]["oil_shutdown"]


def test_electrical_points_map_a_single_phase_meter_to_its_whole_circuit() -> None:
    three = electrical_points({"P1", "P2", "P3", "Ptot", "Wh_Im", "HasAlarm"})
    assert three == {
        "P1": "P_ph",
        "P2": "P_ph",
        "P3": "P_ph",
        "Ptot": "P",
        "Wh_Im": "energy:P",
        "HasAlarm": "alarm",
    }
    single = electrical_points({"P1", "I1", "V1"})
    assert single == {"P1": "P", "I1": "I_1ph", "V1": "V_ln"}

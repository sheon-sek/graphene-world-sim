from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from gws_runtime.instrumentation import Instrumentation
from gws_runtime.network import ControlNetwork
from gws_runtime.values import Quality
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import (
    Access,
    Aggregate,
    Asset,
    AssetCategory,
    AssetSignal,
    ComponentType,
    Connection,
    Domain,
    Endpoint,
    FaultKind,
    FaultModeSpec,
    Instrument,
    InstrumentSource,
    ParameterSpec,
    PointBinding,
    PointClass,
    PointSource,
    PointTemplate,
    PortSpec,
    Site,
    StaticValue,
    Unbound,
    WorldModel,
)

ROOT = Path(__file__).resolve().parents[3]


class State:
    """A StateView over a dict of true values, in SI units."""

    def __init__(self, values: dict[tuple[str, str], float | bool]) -> None:
        self.values = values

    def get(self, asset: str, signal: str) -> float | bool | None:
        return self.values.get((asset, signal))

    def unit(self, asset: str, signal: str) -> str | None:
        return {"TChwLvg": "K", "P": "W"}.get(signal)


def _point(path: str, source: PointSource, unit: str | None = None, **kw: object) -> PointBinding:
    kw.setdefault("point_class", PointClass.PROCESS_VALUE)
    return PointBinding(path=path, data_type="Float4", unit=unit, source=source, **kw)  # type: ignore[arg-type]


def world() -> WorldModel:
    """GW (gateway for Cooling and Electrical) -- SW -- D1. Chiller CH (Cooling) with a
    leaving-temperature sensor TS reporting via GW; meters M1 and M2 (Electrical)."""
    net = ComponentType(
        id="Net",
        name="Net",
        category=AssetCategory.DEVICE,
        parameters={"role": ParameterSpec(default="device"), "serves": ParameterSpec(default="")},
        ports={"net": PortSpec(domain=Domain.NET)},
        point_template={
            "CPU": PointTemplate(
                data_type="Float4", unit="%", point_class=PointClass.NETWORK_STATE
            ),
            "Comm": PointTemplate(data_type="Int4", point_class=PointClass.NETWORK_STATE),
        },
    )
    chiller = ComponentType(
        id="Chiller",
        name="Chiller",
        category=AssetCategory.EQUIPMENT,
        fault_modes={
            "chw_temp_sensor": FaultModeSpec(
                kind=FaultKind.SENSOR,
                parameters={
                    "signal": ParameterSpec(default="TChwLvg"),
                    "bias": ParameterSpec(unit="K", default=1.5),
                },
            ),
            "trip": FaultModeSpec(kind=FaultKind.TRIP),
        },
        point_template={
            "Leaving Temp": PointTemplate(
                data_type="Float4",
                unit="°F",
                point_class=PointClass.PROCESS_VALUE,
                signal="TChwLvg",
            ),
            "Run Cmd": PointTemplate(
                data_type="Boolean",
                point_class=PointClass.COMMAND,
                access=Access.READ_WRITE,
                signal="on",
            ),
        },
    )
    meter = ComponentType(id="Meter", name="Meter", category=AssetCategory.DEVICE)
    assets = [
        Asset(
            id="GW",
            type="Net",
            name="GW",
            parameters={"role": "gateway", "serves": "Cooling, Electrical"},
        ),
        Asset(id="SW", type="Net", name="SW"),
        Asset(id="D1", type="Net", name="D1"),
        Asset(id="CH", type="Chiller", name="CH", system="Cooling"),
        Asset(id="M1", type="Meter", name="M1", system="Electrical"),
        Asset(id="M2", type="Meter", name="M2", system="Electrical"),
    ]
    links = [
        Connection(
            id=f"net:{a}->{b}",
            domain=Domain.NET,
            source=Endpoint(node=a, port="net"),
            target=Endpoint(node=b, port="net"),
        )
        for a, b in (("GW", "SW"), ("SW", "D1"))
    ]
    ts = Instrument(
        id="TS",
        asset="CH",
        quantity="TChwLvg",
        unit="°C",
        range_min=0.0,
        range_max=50.0,
        reports_via=("GW",),
    )
    points = [
        _point("D1/CPU", AssetSignal(asset="D1", signal="CPU"), "%"),
        _point("D1/Comm", AssetSignal(asset="D1", signal="Comm")),
        _point("CH/Leaving Temp", AssetSignal(asset="CH", signal="Leaving Temp"), "°F"),
        _point(
            "CH/Run Cmd",
            AssetSignal(asset="CH", signal="Run Cmd"),
            point_class=PointClass.COMMAND,
            access=Access.READ_WRITE,
        ),
        _point("CH/TS", InstrumentSource(instrument="TS"), "°C"),
        _point("CH/Name", StaticValue(value="CHILLER 1"), point_class=PointClass.STATIC_METADATA),
        _point("CH/Spare", Unbound()),
        _point("M1/Power", AssetSignal(asset="M1", signal="P"), "kW"),
        _point("M2/Power", AssetSignal(asset="M2", signal="P"), "MW"),
        _point("Site/Power", Aggregate(function="sum", inputs=("M1/Power", "M2/Power")), "MW"),
        _point("Site/Share", Aggregate(function="ratio", inputs=("M1/Power", "M2:P")), "%"),
        _point("Site/Mean", Aggregate(function="mean", inputs=("M1/Power", "M2/Power", "TS"))),
    ]
    return WorldModel(
        site=Site(id="t", name="t"),
        component_types={t.id: t for t in (net, chiller, meter)},
        assets={a.id: a for a in assets},
        connections={c.id: c for c in links},
        instruments={"TS": ts},
        point_bindings={p.path: p for p in points},
    )


def true_state(t_chw_c: float = 7.0) -> State:
    return State(
        {
            ("D1", "CPU"): 0.25,
            ("CH", "TChwLvg"): t_chw_c + 273.15,
            ("M1", "P"): 500e3,
            ("M2", "P"): 1.5e6,
        }
    )


def make(seed: int = 7) -> tuple[WorldModel, ControlNetwork, Instrumentation]:
    doc = world()
    net = ControlNetwork.from_world(doc)
    return doc, net, Instrumentation(doc, net, seed)


def test_points_resolve_their_sources_in_their_units() -> None:
    _, _, ins = make()
    ins.update(0.0, true_state())
    p = ins.points()
    assert p["D1/CPU"].value == pytest.approx(25.0)
    assert p["D1/Comm"].value == 0  # from the network model: connected
    assert p["CH/Leaving Temp"].value == pytest.approx(44.6)  # template signal TChwLvg, in degF
    assert ins.reading("TS") == ins.points()["CH/TS"]
    assert p["CH/TS"].value == pytest.approx(7.0)
    assert (p["CH/Name"].value, p["CH/Name"].quality) == ("CHILLER 1", Quality.GOOD)
    assert (p["CH/Spare"].quality, p["CH/Spare"].reason) == (Quality.BAD, "unbound")
    assert (p["CH/Run Cmd"].quality, p["CH/Run Cmd"].reason) == (Quality.BAD, "not_simulated")


def test_aggregates_reconcile_units_and_compute_ratios() -> None:
    _, _, ins = make()
    ins.update(0.0, true_state())
    p = ins.points()
    assert p["Site/Power"].value == pytest.approx(2.0)  # 500 kW + 1.5 MW, in MW
    assert p["Site/Share"].value == pytest.approx(100 * 0.5 / 1.5)  # kW over SI W, in %
    # The mean has no unit of its own: inputs convert to the first input's unit (kW) where the
    # dimension matches; the degC reading does not and passes through unchanged.
    assert p["Site/Mean"].value == pytest.approx((500.0 + 1500.0 + 7.0) / 3)


def test_switch_failure_makes_points_behind_it_stale_and_restore_refreshes_them() -> None:
    _, net, ins = make()
    ins.update(10.0, true_state())
    net.fail("SW")
    net.step(20.0)
    state = true_state()
    state.values[("D1", "CPU")] = 0.9
    ins.update(20.0, state)
    cpu = ins.points()["D1/CPU"]
    assert cpu.value == pytest.approx(25.0)
    assert (cpu.quality, cpu.timestamp, cpu.reason) == (Quality.BAD, 10.0, "comm_lost")
    assert ins.points()["D1/Comm"].value == 1  # the gateway's monitor sees it disconnected
    assert ins.points()["CH/TS"].quality is Quality.GOOD  # not behind the switch
    net.restore("SW")
    net.step(30.0)
    ins.update(30.0, state)
    cpu = ins.points()["D1/CPU"]
    assert cpu.value == pytest.approx(90.0)
    assert (cpu.quality, cpu.timestamp) == (Quality.GOOD, 30.0)


def test_gateway_failure_stales_readings_that_report_through_it() -> None:
    _, net, ins = make()
    ins.update(0.0, true_state())
    net.fail("GW")
    net.step(1.0)
    ins.update(1.0, true_state(9.0))
    for key in ("CH/TS", "CH/Leaving Temp", "M1/Power"):
        s = ins.points()[key]
        assert (s.quality, s.reason, s.timestamp) == (Quality.BAD, "comm_lost", 0.0)
    assert ins.read("TS") is None
    assert ins.points()["Site/Power"].reason == "comm_lost"


@pytest.mark.parametrize(
    ("mode", "params", "expected"),
    [
        ("bias", {"bias": 2.0}, 9.0),
        ("gain", {"gain": 1.1}, 7.7),
        ("drift", {"rate": 0.5}, 8.0),  # two hours after the fault's first update
        ("freeze", {}, 7.0),
    ],
)
def test_sensor_faults_change_the_reading_not_the_true_state(
    mode: str, params: dict[str, float], expected: float
) -> None:
    _, _, ins = make()
    ins.update(0.0, true_state(7.0))
    ins.fault("TS", mode, params)
    ins.update(3600.0, true_state(7.0))
    state = true_state(7.0 if mode != "freeze" else 12.0)
    ins.update(3 * 3600.0, state)
    assert ins.reading("TS").value == pytest.approx(expected)
    assert ins.read("TS") == pytest.approx(expected)
    assert ins.read("CH:TChwLvg") == state.values[("CH", "TChwLvg")]  # true state untouched
    ins.clear("TS")
    ins.update(4 * 3600.0, state)
    assert ins.reading("TS").value == pytest.approx(state.values[("CH", "TChwLvg")] - 273.15)


def test_noise_is_seeded_and_deterministic() -> None:
    runs = []
    for seed in (1, 1, 2):
        _, _, ins = make(seed)
        ins.fault("TS", "noise", {"sigma": 0.5})
        values = []
        for t in (0.0, 1.0, 2.0):
            ins.update(t, true_state())
            values.append(ins.reading("TS").value)
        runs.append(values)
    assert runs[0] == runs[1] != runs[2]
    assert all(v != pytest.approx(7.0) for v in runs[0])


def test_failed_sensor_reads_bad_and_the_bus_sees_none() -> None:
    _, _, ins = make()
    ins.fault("TS", "fail", {})
    ins.update(0.0, true_state())
    assert (ins.reading("TS").quality, ins.reading("TS").reason) == (Quality.BAD, "sensor_failed")
    assert ins.read("TS") is None
    assert ins.points()["CH/TS"].reason == "sensor_failed"


def test_asset_sensor_fault_applies_to_the_signal_its_mode_declares() -> None:
    _, _, ins = make()
    ins.fault("CH", "chw_temp_sensor", {})
    ins.update(0.0, true_state(7.0))
    assert ins.reading("TS").value == pytest.approx(8.5)  # 1.5 K bias in degC
    assert ins.points()["CH/Leaving Temp"].value == pytest.approx(44.6 + 2.7)  # and in degF
    with pytest.raises(ValueError):
        ins.fault("CH", "trip", {})


def test_out_of_range_readings_clamp_as_uncertain() -> None:
    _, _, ins = make()
    ins.update(0.0, true_state(60.0))
    s = ins.reading("TS")
    assert (s.value, s.quality, s.reason) == (50.0, Quality.UNCERTAIN, "out_of_range")


def test_command_points_resolve_to_the_model_input_they_drive() -> None:
    _, _, ins = make()
    assert ins.command_target("CH/Run Cmd") == ("CH", "on")
    assert ins.command_target("CH/Leaving Temp") is None


def test_snapshot_and_restore_continue_identically() -> None:
    _, _, a = make()
    a.fault("TS", "drift", {"rate": 1.0})
    a.fault("CH/TS", "noise", {"sigma": 0.2})
    a.update(0.0, true_state())
    a.update(60.0, true_state())
    _, _, b = make()
    b.restore(a.snapshot())
    a.update(120.0, true_state())
    b.update(120.0, true_state())
    assert a.points() == b.points()


# --- revision 1 ------------------------------------------------------------------------------


class Nothing:
    def get(self, asset: str, signal: str) -> float | bool | None:
        return None

    def unit(self, asset: str, signal: str) -> str | None:
        return None


@pytest.fixture(scope="module")
def revision_1() -> WorldModel:
    return build(Sources.read(ROOT / "data" / "graphene"))


def test_revision_1_serves_every_point_and_a_gateway_failure_stales_its_systems(
    revision_1: WorldModel,
) -> None:
    doc = revision_1
    net = ControlNetwork.from_world(doc)
    ins = Instrumentation(doc, net, seed=1)
    ins.update(0.0, Nothing())
    points = ins.points()
    assert points.keys() == doc.point_bindings.keys()
    counts = Counter((s.quality.value, s.reason) for s in points.values())
    print(dict(counts))
    for path, p in doc.point_bindings.items():
        if isinstance(p.source, StaticValue):
            assert points[path].quality is Quality.GOOD, path
    assert ins.converter.unknown == set()

    net.fail("Network Topology/GATEWAY A")
    net.step(1.0)
    ins.update(1.0, Nothing())
    points = ins.points()
    cooling = [
        path
        for path, p in doc.point_bindings.items()
        if isinstance(p.source, AssetSignal) and doc.assets[p.source.asset].system == "Cooling"
    ]
    assert cooling
    assert {points[p].reason for p in cooling} == {"comm_lost"}
    electrical = next(
        path
        for path, p in doc.point_bindings.items()
        if isinstance(p.source, AssetSignal) and doc.assets[p.source.asset].system == "Electrical"
    )
    assert points[electrical].reason == "not_simulated"

from __future__ import annotations

from pathlib import Path

import pytest

from gws_runtime.conditions import Conditions
from gws_runtime.services import Env, SiteServices
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.model import WorldModel

DATA = Path(__file__).resolve().parents[3] / "data" / "graphene"
CWS = "Cold Water and Sanitary System/"
TOWERS = [f"Cooling Towers Plant/R_P{p}_CT{n}" for p in (1, 2) for n in range(1, 11)]


@pytest.fixture(scope="module")
def site() -> WorldModel:
    return build(Sources.read(DATA))


class Site:
    """The site services on their own, with the rest of the site's state set by hand."""

    def __init__(self, doc: WorldModel) -> None:
        self.services = SiteServices.from_world(doc, doc.assets.keys())
        self.conditions = Conditions.from_world(doc.conditions)
        self.state: dict[str, dict[str, float | bool | str]] = {}
        self.dead: set[str] = set()
        self.t = 0.0

    def env(self, dt: float) -> Env:
        return Env(self.t, dt, lambda a: 0.0 if a in self.dead else 1.0, self.state, lambda a: True)

    def step(self, n: int = 1, dt: float = 1.0) -> dict[str, dict[str, float | bool | str]]:
        for _ in range(n):
            self.t += dt
            self.services.step(self.env(dt), self.conditions)
        return self.services.signals(self.env(dt), self.conditions)


def test_tower_makeup_draws_the_roof_tanks_down_and_the_transfer_pumps_refill_them(
    site: WorldModel,
) -> None:
    s = Site(site)
    for tower in TOWERS:  # 0.5 MW rejected per tower
        s.state[tower] = {"m_flow": 24.0, "TEnt": 308.15, "TLvg": 303.15}
    out = s.step()
    makeup = out["Cooling Towers Plant/R_P1_P1"]
    evaporation = 24.0 * 4184 * 5 / 2.43e6
    assert makeup["m_flow"] == pytest.approx(evaporation * 4 / 3)
    assert makeup["running"] and makeup["p_dis"] == 300e3
    assert out[CWS + "R_BP1"]["running"] and not out[CWS + "R_BP2"]["running"]
    assert out[CWS + "R_BP1"]["m_flow"] == pytest.approx(20 * makeup["m_flow"])
    assert not out[CWS + "G_TP1"]["running"]

    level = out[CWS + "R_T1"]["level"]
    out = s.step(600, dt=5.0)
    assert any(out[CWS + f"G_TP{n}"]["running"] for n in (1, 2, 3))
    assert 0.2 <= out[CWS + "R_T1"]["level"] <= 0.3  # held in the gateway's alarm band
    assert out[CWS + "R_T1"]["level"] != level and not out[CWS + "R_T1"]["alarm"]
    assert 0.2 <= out[CWS + "G_T1"]["level"] <= 0.3  # the main refills the ground tanks

    s.services.fault(CWS + "R_BP1", "trip", {})
    s.services.fault(CWS + "R_BP2", "trip", {})
    out = s.step()
    assert out[CWS + "R_BP1"]["alarm"] and not out[CWS + "R_BP1"]["running"]
    makeup = out["Cooling Towers Plant/R_P1_P1"]
    assert makeup["low_suction"] and makeup["alarm"] and makeup["m_flow"] == 0.0


def test_losing_the_main_drains_the_ground_tanks_into_the_roof(site: WorldModel) -> None:
    s = Site(site)
    for tower in TOWERS:
        s.state[tower] = {"m_flow": 24.0, "TEnt": 308.15, "TLvg": 303.15}
    s.conditions.set({"water_mains_available": False})
    start = s.step()[CWS + "G_T1"]["level"]
    out = s.step(2000, dt=10.0)
    assert out[CWS + "G_T1"]["level"] < start - 0.05
    assert out[CWS + "G_T1"]["alarm"]


def test_a_fire_alarms_its_zone_which_shuts_its_pahu_and_recalls_the_lifts(
    site: WorldModel,
) -> None:
    s = Site(site)
    s.conditions.set({"lift_trips_per_hour": 120.0})
    s.step(120)
    assert not s.services.fire_shutdown("PAHU/L1_PAHU2")
    s.conditions.set({"fires": {"DH02": {"smoke_pct_m": 5.0, "temperature_c": 90.0}}})
    out = s.step()
    assert out["~L1-Z2-SD1"]["alarm"] and out["~FZ-L1-Z2"]["alarm"]
    assert s.services.fire_shutdown("PAHU/L1_PAHU2")
    assert not s.services.fire_shutdown("PAHU/L1_PAHU1")  # another zone's PAHU runs on
    assert not out["~L1-Z2-AV1"]["operated"]  # the retard delay
    out = s.step(60)
    assert out["~L1-Z2-AV1"]["operated"] and out["~L1-SA-FP1"]["running"]
    assert s.services.demand_w("~L1-SA-FP1") == 110e3
    for lift in ("~LIFT-1", "~LIFT-2", "~LIFT-3"):
        assert out[lift]["level"] == 1.0 and out[lift]["doorState"] == 1.0
        assert out[lift]["direction"] == ""

    s.conditions.set({"fires": {}})
    s.services.command("~FZ-L1-Z2", "reset", True)
    out = s.step()
    assert out["~FZ-L1-Z2"]["alarm"]  # the alarm valve still reports flow
    s.step(5)
    s.services.command("~FZ-L1-Z2", "reset", True)
    out = s.step()
    assert not out["~FZ-L1-Z2"]["alarm"] and out["~L1-SA-FP1"]["running"]  # stopped by hand
    s.services.command("~L1-SA-FP1", "stop", True)
    assert not s.step()["~L1-SA-FP1"]["running"]


def test_lifts_serve_calls_and_a_dead_lift_stops(site: WorldModel) -> None:
    s = Site(site)
    s.conditions.set({"lift_trips_per_hour": 360.0})
    seen: set[tuple[float | bool | str, float | bool | str]] = set()
    for _ in range(600):
        out = s.step()
        seen.add((out["~LIFT-1"]["level"], out["~LIFT-1"]["direction"]))
    assert {level for level, _ in seen} == {1.0, 2.0, 3.0, 4.0}
    assert {d for _, d in seen} == {"", "Up", "Down"}
    s.dead.add("~LIFT-1")
    out = s.step(30)
    assert out["~LIFT-1"]["direction"] == "" and out["~LIFT-1"]["doorState"] == 0.0
    assert s.services.demand_w("~LIFT-1") == 1.5e3


def test_genset_day_tanks_drain_without_fuel_transfer(site: WorldModel) -> None:
    s = Site(site)
    s.state["Genset/Genset 1"] = {"running": True, "P": 3.0e6}
    out = s.step(120, dt=10.0)  # 20 minutes at full load uses a fifth of the day tank
    assert out["Diesel/Tank 1"]["fuel_total"] > 0 and out["Diesel/Tank 1"]["level"] < 0.9
    assert s.services.fuel_ok("Genset/Genset 1")
    s.services.fault("Diesel/Tank 1", "contamination", {})
    for _ in range(100):
        s.step(60, dt=10.0)
        if not s.services.fuel_ok("Genset/Genset 1"):
            break
    assert not s.services.fuel_ok("Genset/Genset 1")
    out = s.step()
    assert out["Diesel/Tank 1"]["alarm"] and out["Diesel/Tank 1"]["fuel_flow"] == 0.0
    assert s.services.fuel_ok("Genset/Genset 2")  # its own day tank, unused


def test_a_leak_is_found_on_the_cable_that_covers_it(site: WorldModel) -> None:
    s = Site(site)
    cables = [f"Water Leak Detection System/Ground/1{c}" for c in "ABC"]
    x, y = (site.assets[cables[0]].location.x or 0.0), (site.assets[cables[0]].location.y or 0.0)
    s.conditions.set({"leaks": {"G-WTR": {"flow_kg_s": 0.2, "x": x + 30.0, "y": y}}})
    out = s.step()
    assert all(out[c]["leak_position"] == 0.0 for c in cables)  # not yet spread to a cable
    out = s.step(10)
    assert out[cables[1]]["leak_position"] == pytest.approx(10.0)
    assert out[cables[0]]["leak_position"] == 0.0 and out[cables[2]]["leak_position"] == 0.0
    s.services.fault(cables[1], "cable_fault", {})
    out = s.step()
    assert out[cables[1]]["status"] == 1.0 and out[cables[1]]["leak_position"] == 0.0

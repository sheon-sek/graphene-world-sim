"""Phase 3 gate: the Phase 1 slice runs from World Model data through the runtime, with
faults, controllers and instrumentation.

    uv run python -m gws_runtime.gate data/graphene docs/reports/phase3-gate

runs one session on the slice — revision 1 of the site imported from the source data — and
writes `<out>.json` and `<out>.md`. Each scenario injects one cause and checks a consequence
that only the models or the controllers can produce. Needs OpenModelica (see ADR-0002) the
first time, to compile the slice; afterwards the FMU cache serves it.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gws_runtime.compiler import CACHE
from gws_runtime.lifecycle import Session
from gws_world_model.importers.graphene import Sources, build

SLICE: tuple[str, ...] = (
    "Chiller/R_C1",
    "Chiller/R_CP1",
    "Chiller/R_CV1",
    "Chiller/R_CV9",
    "Buffer Tank/R_BT1",
    "Buffer Tank/R_BT2",
    "Chiller/R_CP9",
    "~CB-001",
    "FCU/L1_FCU1",
    "~CCU-001",
    "Chiller/R_CP5",
    "Chiller/R_CV5",
    *(f"Cooling Towers Plant/R_P1_CT{i}" for i in range(1, 6)),
    "~IT-DH01",
)
"""The Phase 1 slice: chiller 1 with its legs, the buffer tanks, the secondary pump, cooling
block 1 and the two DH01 units it serves, five towers, and DH01's IT load."""

IT_FRACTION = 0.3
"""DH01 runs at 30 % of its 1,000 kW design load: the slice models only FCU1 (150 kW) and
CCU-001 (200 kW) of the hall's cooling, so the full 447 kW operating load would overheat it."""
DT = 5.0
C1, CP9, TOWER = "Chiller/R_C1", "Chiller/R_CP9", "Cooling Towers Plant/R_P1_CT1"
ROOM = "room:DH01"


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Scenario:
    name: str
    cause: str
    checks: list[Check] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)


def _c(kelvin: float | bool | str) -> float:
    assert isinstance(kelvin, float | int)
    return round(float(kelvin) - 273.15, 2)


class Gate:
    def __init__(self, data: Path, cache: Path = CACHE) -> None:
        doc = build(Sources.read(data))
        t0 = time.perf_counter()
        self.session = Session(doc, SLICE, dt=DT, cache=cache, revision=1)
        self.init_s = time.perf_counter() - t0
        self.scenarios: list[Scenario] = []
        self.wall_s = 0.0

    @property
    def sim(self) -> Any:
        return self.session.sim

    def s(self, asset: str, signal: str) -> Any:
        return self.sim.state[asset].get(signal)

    def run_min(self, minutes: float) -> None:
        t0 = time.perf_counter()
        self.session.step(round(minutes * 60 / DT))
        self.wall_s += time.perf_counter() - t0

    def scenario(self, name: str, cause: str, body: Callable[[Scenario], None]) -> None:
        sc = Scenario(name, cause)
        body(sc)
        self.scenarios.append(sc)

    def check(self, sc: Scenario, name: str, passed: bool, detail: str) -> None:
        sc.checks.append(Check(name, bool(passed), detail))

    # --- scenarios -------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        apply = self.session.apply
        apply("conditions", {"changes": {"it_fraction": {"DH01": IT_FRACTION}}})
        self.run_min(60)

        def baseline(sc: Scenario) -> None:
            supply = self.sim.instrumentation.reading("HDR/TS-02").value
            dp = self.sim.instrumentation.reading("HDR/DPS-01").value
            assert isinstance(supply, float) and isinstance(dp, float)
            cw = _c(self.s(C1, "TCwEnt"))
            sc.values = {
                "chiller_leaving_C": _c(self.s(C1, "TChwLvg")),
                "header_supply_reading_C": supply,
                "loop_dp_reading_kPa": dp,
                "cw_entering_C": cw,
                "room_C": _c(self.s(ROOM, "TAir")),
                "chiller_kW": round(self.s(C1, "P") / 1e3, 1),
            }
            self.check(sc, "dp loop holds 85 kPa", abs(dp - 85) < 2, f"{dp:.1f} kPa")
            self.check(sc, "header supply at 14 °C", abs(supply - 14) < 0.3, f"{supply:.2f} °C")
            self.check(sc, "condenser water at 29 °C", abs(cw - 29) < 0.5, f"{cw} °C")

        self.scenario("Steady state", "DH01 at 30 % IT load for an hour", baseline)

        def chiller_trip(sc: Scenario) -> None:
            room0 = _c(self.s(ROOM, "TAir"))
            fault = apply("fault", {"target": C1, "mode": "trip"})
            self.run_min(10)
            room1 = _c(self.s(ROOM, "TAir"))
            self.check(
                sc,
                "chiller stops",
                self.s(C1, "running") is False,
                f"running={self.s(C1, 'running')}",
            )
            self.check(sc, "hall warms", room1 > room0 + 0.5, f"{room0} → {room1} °C")
            self.check(
                sc,
                "PLC sees the trip",
                self.sim.instrumentation.read("Chiller/R_C1/System Failure_Trip") is True,
                "System Failure_Trip point",
            )
            apply("clear", {"id": fault.id})
            self.run_min(2)
            self.check(
                sc,
                "trip latches after the cause clears",
                self.s(C1, "running") is False,
                "still stopped",
            )
            apply("reset", {"target": C1})
            self.run_min(20)
            room2 = _c(self.s(ROOM, "TAir"))
            self.check(
                sc,
                "restarts after reset and recovers",
                self.s(C1, "running") is True and room2 < room1,
                f"{room1} → {room2} °C",
            )
            sc.values = {"room_before_C": room0, "room_tripped_C": room1, "room_recovered_C": room2}

        self.scenario("Chiller trip", "trip on Chiller/R_C1, clear, reset", chiller_trip)

        def sensor_bias(sc: Scenario) -> None:
            true0 = _c(self.s(CP9, "TEnt"))
            fault = apply(
                "fault", {"target": "HDR/TS-02", "mode": "bias", "parameters": {"bias": 1.5}}
            )
            self.run_min(30)
            true1 = _c(self.s(CP9, "TEnt"))
            reading = self.sim.instrumentation.reading("HDR/TS-02").value
            self.check(
                sc,
                "controller trims to the wrong reading",
                true1 < true0 - 1.0,
                f"true supply {true0} → {true1} °C while the sensor reads {reading:.2f} °C",
            )
            apply("clear", {"id": fault.id})
            self.run_min(30)
            sc.values = {
                "true_supply_before_C": true0,
                "true_supply_biased_C": true1,
                "reading_C": reading,
            }

        self.scenario("Sensor bias", "+1.5 K bias on header supply sensor HDR/TS-02", sensor_bias)

        def wet_bulb(sc: Scenario) -> None:
            fan0 = self.s(TOWER, "PFan")
            p0 = self.s(C1, "P")
            apply("conditions", {"changes": {"dry_bulb_c": 34.0, "wet_bulb_c": 29.0}})
            self.run_min(30)
            fan1, p1 = self.s(TOWER, "PFan"), self.s(C1, "P")
            cw1 = _c(self.s(C1, "TCwEnt"))
            self.check(
                sc,
                "towers work harder",
                fan1 > fan0 * 1.5,
                f"fan {fan0 / 1e3:.2f} → {fan1 / 1e3:.2f} kW",
            )
            self.check(
                sc,
                "chiller works harder",
                p1 > p0,
                f"{p0 / 1e3:.1f} → {p1 / 1e3:.1f} kW, CW entering {cw1} °C",
            )
            apply("conditions", {"changes": {"wet_bulb_c": 25.0, "dry_bulb_c": 30.0}})
            self.run_min(20)
            sc.values = {"fan_kW": [fan0 / 1e3, fan1 / 1e3], "chiller_kW": [p0 / 1e3, p1 / 1e3]}

        self.scenario("Hot humid day", "wet bulb 25 → 29 °C", wet_bulb)

        def fouling(sc: Scenario) -> None:
            p0, q0 = self.s(C1, "P"), self.s(C1, "QEva")
            fault = apply(
                "fault",
                {
                    "target": C1,
                    "mode": "condenser_fouling",
                    "parameters": {"cop_fraction": 0.6},
                    "ramp_s": 600,
                },
            )
            self.run_min(20)
            p1, q1 = self.s(C1, "P"), self.s(C1, "QEva")
            self.check(
                sc,
                "same duty at more power (warm rebuild)",
                p1 > p0 * 1.3 and abs(q1 - q0) < 0.15 * q0,
                f"{p0 / 1e3:.0f} → {p1 / 1e3:.0f} kW, cooling {q0 / 1e3:.0f} → {q1 / 1e3:.0f} kW",
            )
            apply("clear", {"id": fault.id})
            self.run_min(10)
            sc.values = {"chiller_kW": [p0 / 1e3, p1 / 1e3]}

        self.scenario("Condenser fouling", "COP to 60 %, ramped over 10 min", fouling)

        def pump_trip(sc: Scenario) -> None:
            room0 = _c(self.s(ROOM, "TAir"))
            flow0 = self.s("~CB-001", "m_flow")
            fault = apply("fault", {"target": CP9, "mode": "trip"})
            self.run_min(10)
            flow = self.s("~CB-001", "m_flow")
            room1 = _c(self.s(ROOM, "TAir"))
            # The primary pump still pushes some water through the stopped pump, which has no
            # check valve in the model: flow collapses rather than stopping.
            self.check(
                sc,
                "secondary flow collapses",
                flow < 0.3 * flow0,
                f"cooling block flow {flow0:.1f} → {flow:.1f} kg/s",
            )
            self.check(sc, "hall warms", room1 > room0 + 0.5, f"{room0} → {room1} °C")
            apply("clear", {"id": fault.id})
            apply("reset", {"target": CP9})
            self.run_min(20)

        self.scenario("Secondary pump trip", "trip on Chiller/R_CP9", pump_trip)

        def utility_loss(sc: Scenario) -> None:
            apply("conditions", {"changes": {"utility_available": False}})
            v_min, it_min, stopped = 1.0, 1e9, False
            for _ in range(12):
                self.run_min(5 / 60 * 1)  # one step
                v_min = min(v_min, self.s(C1, "V_pu"))
                it_min = min(it_min, self.s("~IT-DH01", "P"))
                stopped |= self.s(C1, "running") is False
            self.run_min(5)
            source = self.sim.state["~ATS-A"]["source"]
            self.check(
                sc,
                "chiller loses supply, then runs on the gensets",
                stopped and self.s(C1, "running") is True,
                f"lowest chiller supply {v_min:.2f} pu, ATS-A source {source}",
            )
            self.check(
                sc, "IT load never drops (UPS)", it_min > 0, f"lowest IT draw {it_min / 1e3:.0f} kW"
            )
            apply("conditions", {"changes": {"utility_available": True}})
            self.run_min(10)

        self.scenario("Utility loss", "utility supply lost", utility_loss)

        def network(sc: Scenario) -> None:
            speed0 = self.sim.commands[CP9]["speed"]
            apply("fault", {"target": "Network Topology/GATEWAY A", "mode": "failure"})
            self.run_min(2)
            reading = self.sim.instrumentation.reading("HDR/DPS-01")
            self.check(
                sc,
                "cooling readings lose comms",
                reading.quality.value == "bad" and reading.reason == "comm_lost",
                f"HDR/DPS-01 {reading.quality.value} {reading.reason}",
            )
            self.check(
                sc,
                "dp loop holds its output",
                self.sim.commands[CP9]["speed"] == speed0,
                f"CP9 speed held at {speed0:.3f}",
            )
            self.run_min(1)

        self.scenario("Gateway failure", "GATEWAY A fails", network)

        t0 = time.perf_counter()
        final = self.sim.frame().to_json()
        again = self.session.replay()
        replayed = again.sim.frame().to_json()
        replay_s = time.perf_counter() - t0
        again.close()
        sc = Scenario("Replay", "re-run the event log from the start")
        self.check(
            sc,
            "identical trajectory",
            replayed == final,
            f"{len(self.session.events)} events, step {final['step']}",
        )
        self.scenarios.append(sc)

        sim_s = self.sim.t
        result = {
            "scope": list(SLICE),
            "partitions": {n: len(p.assets) for n, p in self.sim.partitions.items()},
            "not_modelled": self.sim.plan.not_modelled,
            "controllers": [b.binding.id for b in self.sim.blocks],
            "dt_s": DT,
            "simulated_h": round(sim_s / 3600, 2),
            "wall_s": round(self.wall_s, 1),
            "real_time_factor": round(sim_s / max(self.wall_s, 1e-9)),
            "init_s": round(self.init_s, 1),
            "replay_s": round(replay_s, 1),
            "passed": all(c.passed for s in self.scenarios for c in s.checks),
            "scenarios": [
                {
                    "name": s.name,
                    "cause": s.cause,
                    "checks": [c.__dict__ for c in s.checks],
                    "values": s.values,
                }
                for s in self.scenarios
            ],
        }
        self.session.close()
        return result


def markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Phase 3 gate: the slice through the runtime",
        "",
        f"Result: **{'PASS' if result['passed'] else 'FAIL'}**. "
        f"{result['simulated_h']} simulated hours at a {result['dt_s']:g} s macro step in "
        f"{result['wall_s']} s ({result['real_time_factor']}× real time); session start "
        f"{result['init_s']} s with the FMU cached; replay {result['replay_s']} s.",
        "",
        f"Scope: {len(result['scope'])} assets, partitions "
        + ", ".join(f"`{k}` ({v} assets)" for k, v in result["partitions"].items())
        + ". Controllers: "
        + ", ".join(f"`{c}`" for c in result["controllers"])
        + ".",
        "",
        f"DH01 runs at {IT_FRACTION:.0%} of design IT load: the slice holds only FCU1 and "
        "CCU-001 of the hall's cooling.",
        "",
        "| Scenario | Cause | Check | Result | Evidence |",
        "|---|---|---|---|---|",
    ]
    for s in result["scenarios"]:
        for c in s["checks"]:
            lines.append(
                f"| {s['name']} | {s['cause']} | {c['name']} | "
                f"{'pass' if c['passed'] else 'FAIL'} | {c['detail']} |"
            )
    return "\n".join(lines) + "\n"


def main() -> None:
    data, out = Path(sys.argv[1]), Path(sys.argv[2])
    result = Gate(data).run()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    out.with_suffix(".md").write_text(markdown(result), encoding="utf-8")
    print(markdown(result))
    sys.exit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()

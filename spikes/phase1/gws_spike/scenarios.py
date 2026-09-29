"""Phase 1 spike scenarios: steady state, three injected causes, and a structural rebuild.

Usage: python -m gws_spike.scenarios <work-dir>

The work dir receives the generated models, FMUs, build logs and results.json.
"""

from __future__ import annotations

import copy
import json
import multiprocessing
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from gws_spike.compile import compile_fmu
from gws_spike.cosim import Fault, Plant
from gws_spike.generate import generate

HERE = Path(__file__).resolve().parent.parent
FRAGMENT = HERE / "worldmodel" / "chiller_leg_dh01.json"

WATCH = {
    "hall_air_C": "Room_DH01_TAir",
    "chw_supply_C": "Chiller_R_C1_TChwLvg",
    "cw_leaving_chiller_C": "Chiller_R_C1_TCwLvg",
    "tower1_leaving_C": "Cooling_Towers_Plant_R_P1_CT1_TLvg",
    "chiller_kW": "Chiller_R_C1_P",
    "chiller_cooling_kW": "Chiller_R_C1_QEva",
    "fcu_cooling_kW": "FCU_L1_FCU1_Q",
    "chw_flow_kg_s": "Chiller_R_CP1_m_flow",
    "cw_flow_kg_s": "Chiller_R_CP5_m_flow",
    "chiller_running": "Chiller_R_C1_running",
}


def sample(plant: Plant) -> dict[str, float]:
    raw = plant.read(WATCH.values())
    row: dict[str, float] = {"t_s": plant.time}
    for key, var in WATCH.items():
        v = raw[var]
        if key.endswith("_C"):
            v -= 273.15
        elif key.endswith("_kW"):
            v /= 1000
        row[key] = round(v, 3)
    row["mcc_ch1_V_pu"] = round(plant.volts.get("MCC-CH1", float("nan")), 4)
    return row


def build(fragment: dict[str, Any], work: Path) -> tuple[Path, dict[str, Any], float]:
    source, point_map = generate(fragment)
    out = work / point_map["model"]
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{point_map['model']}.mo").write_text(source)
    fmu, seconds = compile_fmu(out, point_map["model"])
    return fmu, point_map, seconds


def run(plant: Plant, seconds: int, every: int = 60) -> list[dict[str, float]]:
    rows = []
    for k in range(seconds):
        plant.step(1.0)
        if k % every == 0 or k == seconds - 1:
            rows.append(sample(plant))
    return rows


def warm(
    fmu: Path,
    point_map: dict[str, Any],
    fragment: dict[str, Any],
    state: dict[str, dict[str, float]] | None,
) -> Plant:
    plant = Plant(str(fmu), point_map, fragment, start_params=state)
    if state is None:
        run(plant, 4 * 3600, every=3600)
    return plant


def isolated(fn: Callable[..., Any], *args: Any) -> Any:
    """Run fn in a fresh process. The OpenModelica FMU runtime keeps a process-wide memory
    pool that is not released by fmi2FreeInstance, so many instantiate/free cycles in one
    process eventually abort; one process per plant instance avoids it."""
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx) as pool:
        return pool.submit(fn, *args).result()


def fault_scenario(
    fmu: Path,
    point_map: dict[str, Any],
    fragment: dict[str, Any],
    state: dict[str, dict[str, float]],
    fault: Fault,
) -> dict[str, Any]:
    """15 min healthy, 15 min faulted, 30 min after the fault clears."""
    plant = warm(fmu, point_map, fragment, state)
    t0 = time.perf_counter()
    rows = run(plant, 900)
    plant.inject(fault)
    rows += run(plant, 900, every=30)
    plant.clear(fault.target)
    rows += run(plant, 1800)
    wall = time.perf_counter() - t0
    solves, pf_s = plant.pf_solves, plant.pf_seconds
    plant.close()
    return {
        "fault": fault.__dict__,
        "wall_s": round(wall, 2),
        "pf_solves": solves,
        "pf_s": round(pf_s, 2),
        "rows": rows,
    }


def steady_state(fmu: Path, point_map: dict[str, Any], fragment: dict[str, Any]) -> dict[str, Any]:
    """Warm up from default start values, snapshot, and check a restored copy tracks it."""
    plant = Plant(str(fmu), point_map, fragment)
    t0 = time.perf_counter()
    rows = run(plant, 4 * 3600, every=1800)
    out: dict[str, Any] = {
        "warmup": {
            "sim_s": 4 * 3600,
            "wall_s": round(time.perf_counter() - t0, 2),
            "pf_solves": plant.pf_solves,
            "pf_s": round(plant.pf_seconds, 2),
            "rows": rows,
        }
    }
    steady = plant.snapshot()
    restored = Plant(str(fmu), point_map, fragment, start_params=steady, start_time=plant.time)
    worst: dict[str, float] = {}
    trace = []
    for k in range(600):
        plant.step(1.0)
        restored.step(1.0)
        a, b = sample(plant), sample(restored)
        if k < 30:
            trace.append(
                {
                    "t_s": k + 1,
                    "fcu_kW": a["fcu_cooling_kW"],
                    "fcu_kW_restored": b["fcu_cooling_kW"],
                }
            )
        for key in ("hall_air_C", "chw_supply_C", "cw_leaving_chiller_C", "fcu_cooling_kW"):
            worst[key] = round(max(worst.get(key, 0.0), abs(a[key] - b[key])), 3)
    out["restore_check"] = {"sim_s": 600, "max_abs_difference": worst, "first_30_s": trace}
    out["steady"] = steady
    restored.close()
    plant.close()
    return out


def rebuild_scenario(
    fmu: Path,
    point_map: dict[str, Any],
    fragment: dict[str, Any],
    steady: dict[str, dict[str, float]],
    work: Path,
) -> dict[str, Any]:
    """Add a sixth tower cell to the condenser loop while the plant runs.

    The new model compiles in the background while the running plant keeps stepping; then
    the state moves across by World Model asset id and the new plant takes over.
    """
    changed = copy.deepcopy(fragment)
    changed["name"] = "ChillerLegDH01v2"
    new_tower = "Cooling Towers Plant/R_P1_CT6"
    changed["assets"].append(
        {
            "id": new_tower,
            "type": "Cooling Tower",
            "params": {"m_flow_nominal": 9.488},
            "inputs": {"fanSpeed": 1.0},
        }
    )
    changed["connections"] += [
        {"medium": "cw", "from": "Chiller/R_C1.cw_out", "to": f"{new_tower}.inlet"},
        {"medium": "cw", "from": f"{new_tower}.outlet", "to": "Chiller/R_CP5.inlet"},
    ]
    changed["electrical"]["loads"][new_tower] = "MCC-CT1"

    old = Plant(str(fmu), point_map, fragment, start_params=steady)
    built: dict[str, Any] = {}
    worker = threading.Thread(
        target=lambda: built.update(zip(("fmu", "map", "secs"), build(changed, work), strict=True))
    )
    worker.start()
    before: list[dict[str, float]] = []
    while worker.is_alive():
        before += run(old, 60, every=60)
    worker.join()
    last = sample(old)
    state = old.snapshot()
    t_swap = time.perf_counter()
    new = Plant(str(built["fmu"]), built["map"], changed, start_params=state, start_time=old.time)
    swap_s = time.perf_counter() - t_swap
    old.close()
    after = run(new, 60, every=1) + run(new, 3540, every=300)
    new.close()
    return {
        "change": "add tower cell R_P1_CT6 to the CW loop",
        "compile_s": round(built["secs"], 1),
        "old_plant_kept_running_sim_s": len(before) * 60,
        "swap_s": round(swap_s, 3),
        "at_swap_old": last,
        "rows": after,
    }


def main() -> None:
    work = Path(sys.argv[1]).resolve()
    work.mkdir(parents=True, exist_ok=True)
    fragment = json.loads(FRAGMENT.read_text())
    results: dict[str, Any] = {}

    fmu, point_map, secs = build(fragment, work)
    results["compile_s"] = round(secs, 1)
    results.update(isolated(steady_state, fmu, point_map, fragment))
    steady = results.pop("steady")
    (work / "steady.json").write_text(json.dumps(steady, indent=1))
    (work / "results.json").write_text(json.dumps(results, indent=1))

    results["faults"] = [
        isolated(fault_scenario, fmu, point_map, fragment, steady, f)
        for f in (
            Fault("CB-MCC-CH1", "open"),
            Fault("Chiller/R_CP1", "trip"),
            Fault("Cooling Towers Plant/R_P1_CT1", "trip"),
        )
    ]
    (work / "results.json").write_text(json.dumps(results, indent=1))
    results["rebuild"] = isolated(rebuild_scenario, fmu, point_map, fragment, steady, work)
    (work / "results.json").write_text(json.dumps(results, indent=1))
    print(json.dumps(results["restore_check"]["max_abs_difference"]))


if __name__ == "__main__":
    main()

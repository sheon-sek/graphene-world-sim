"""Collect the graded Phase 8 answers into the evidence file of the report.

    uv run python tests/agents/report.py docs/reports/phase8-agents.json

Reads every graded `.agents/results/<scenario>.json` (a run record with the analyst's answer,
its tool calls, the grade and the findings) and writes them in scenario order, with the
scenario's truth beside each answer and a tally of the grades. Runs set aside under another
name (`<scenario>.<label>.json`, for example one made before a fix) are kept as `earlier`.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ignition"))

import evaluate  # noqa: E402
from gateway import ROOT  # noqa: E402

from gws_runtime.scenarios import ScenarioSet  # noqa: E402

KEEP = ("scenario", "speed", "baseline_start", "end", "events", "answer", "tool_calls", "grade")


def _earlier(path: Path, scenario: str) -> dict[str, Any]:
    record = json.loads(path.read_text())
    label = path.name.removeprefix(f"{scenario}.").removesuffix(".json")
    return {
        "label": label,
        **{k: record[k] for k in KEEP if k in record},
        "findings": record.get("findings", []),
    }


def collect(results: Path) -> dict[str, Any]:
    scenarios = ScenarioSet.read(ROOT / "data" / "scenarios" / "incidents.json").scenarios
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        graded = results / f"{scenario.id}.json"
        row: dict[str, Any] = {"scenario": scenario.id, "grade": None}
        if graded.exists():
            record = json.loads(graded.read_text())
            row = {k: record[k] for k in KEEP if k in record}
            row["findings"] = record.get("findings", [])
        row["truth"] = scenario.truth
        row["earlier"] = [
            _earlier(p, scenario.id)
            for p in sorted(results.glob(f"{scenario.id}.*.json"))
            if not p.name.endswith(".run.json")
        ]
        rows.append(row)
    graded_rows = [r for r in rows if r["grade"]]
    tally = {
        part: dict(Counter(r["grade"][part] for r in graded_rows))
        for part in ("root_cause", "impact")
    }
    return {"scenarios": rows, "tally": tally, "graded": len(graded_rows), "total": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    args.out.write_text(json.dumps(collect(evaluate.AGENTS / "results"), indent=2) + "\n")


if __name__ == "__main__":
    main()

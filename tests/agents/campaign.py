"""Run the Phase 8 evaluation over many scenarios, unattended: for each one, play it into a
fresh gateway (`evaluate.py`), then hand its brief to an analyst agent (Claude Code in print
mode) whose only door into the plant is `tests/agents/mcp.py`, and keep its answer.

    uv run python tests/agents/campaign.py [scenario ...]    # default: every scenario

Answers land in `.agents/results/<scenario>.md` next to the run record `<scenario>.run.json`;
grading them against the scenarios' truth is a separate, reviewed step. A scenario that
already has an answer is skipped, so an interrupted campaign resumes where it stopped.
`--analyse-only` skips the run and analyses whatever `.agents/run.json` holds.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ignition"))

import evaluate  # noqa: E402
from gateway import ROOT, _wait  # noqa: E402

from gws_runtime.scenarios import ScenarioSet  # noqa: E402

RESULTS = evaluate.AGENTS / "results"
ANALYST_TIMEOUT_S = 3600
TOOLS = ["Bash", "Read", "Write", "Glob", "Grep"]


def analyse(record: dict[str, object]) -> Path:
    brief = (evaluate.AGENTS / "brief.md").read_text()
    RESULTS.mkdir(parents=True, exist_ok=True)
    scenario = str(record["scenario"])
    (RESULTS / f"{scenario}.run.json").write_text(json.dumps(record, indent=2))
    claude = shutil.which("claude") or "claude"
    done = subprocess.run(
        [
            claude,
            "-p",
            brief,
            "--output-format",
            "text",
            "--allowedTools",
            *TOOLS,
            "--disallowedTools",
            "WebFetch",
            "WebSearch",
        ],
        cwd=Path.home(),
        capture_output=True,
        text=True,
        timeout=ANALYST_TIMEOUT_S,
        check=False,
    )
    out = RESULTS / f"{scenario}.md"
    failed = f"\n\n[analyst exit {done.returncode}]\n{done.stderr}" if done.returncode else ""
    out.write_text(done.stdout + failed)
    return out


def ensure_docker() -> None:
    """A cloud container that was reclaimed comes back without its Docker daemon."""
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0:
        return
    subprocess.Popen(["dockerd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait(
        "docker",
        120,
        lambda: subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        == 0,
        2,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenarios", nargs="*")
    parser.add_argument("--analyse-only", action="store_true")
    parser.add_argument("--baseline-s", type=float, default=300.0)
    parser.add_argument("--speed", type=float, default=10.0)
    args = parser.parse_args()
    if args.analyse_only:
        print(analyse(json.loads((evaluate.AGENTS / "run.json").read_text())), flush=True)
        return
    ids = args.scenarios or [
        s.id for s in ScenarioSet.read(ROOT / "data/scenarios/incidents.json").scenarios
    ]
    for scenario in ids:
        if (RESULTS / f"{scenario}.md").exists():
            continue  # analysed by an earlier, interrupted campaign
        ensure_docker()
        try:
            record = evaluate.run(scenario, args.baseline_s, args.speed)
        except Exception as e:  # one failed run must not stop the campaign
            print(f"{scenario}: run failed: {type(e).__name__}: {e}", flush=True)
            continue
        print(f"{scenario}: analysed -> {analyse(record)}", flush=True)


if __name__ == "__main__":
    main()

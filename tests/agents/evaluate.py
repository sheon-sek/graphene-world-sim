"""Phase 8 agent evaluation (#70): play an incident scenario into a real Ignition gateway, so an
analyst agent can investigate it through ignition-mcp alone.

    uv run python tests/agents/evaluate.py <scenario-id> [--baseline-s 300]

For one scenario of `data/scenarios/incidents.json` it:

1. rebuilds the igdev gateway (`igdev gateway reset`, so every run starts on a fresh trial
   licence) with the OPC UA, WebDev, Historian and MCP modules, and a disposable API token;
2. creates a Core Historian provider, starts the simulator, creates a session over the
   scenario scope, applies the scenario's setup and runs the warm-up as fast as it can;
3. serves the session over OPC UA at real time, connects the gateway to it and imports the
   tags of the points the session publishes, each recording its history;
4. installs ignition-mcp's Runtime MCP server on the gateway with its setup CLI (the
   `analysis` role) and writes `.agents/mcp.json` for `tests/agents/mcp.py`;
5. runs `--baseline-s` of normal operation, then the scenario's events at their offsets, then
   its observation window, all at real time, and pauses the session.

The gateway and simulator stay up afterwards so the agents can investigate; the next run
stops the simulator and rebuilds the gateway. `.agents/run.json` records the scenario and its
wall-clock window for the grader; the agents never read it.

It needs what `tests/ignition/live.py` needs, plus an ignition-mcp checkout (`IGNITION_MCP`,
default `../ignition-mcp`) whose setup CLI is installed (`uv sync`). ignition-mcp's setup
refuses a setup key whose security level is not ticked under every permission in
Security > General Settings, Designer included, so the harness ticks it on this disposable
gateway (the owner's decision, 2026-09-30).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ignition"))

import live  # noqa: E402
from gateway import ROOT, Gateway, GatewayError, _wait, igdev  # noqa: E402

from gws_api.ignition import generate  # noqa: E402
from gws_runtime.scenarios import SETUP, ScenarioSet, scope  # noqa: E402
from gws_world_model.importers.graphene import Sources, build  # noqa: E402

HISTORIAN = "GwsHistory"
DEPLOYMENT = "gws-eval"
ROLE = "analysis"
IGNITION_MCP = Path(os.environ.get("IGNITION_MCP", ROOT.parent / "ignition-mcp"))
AGENTS = ROOT / ".agents"
MCP_MODULE = "com.inductiveautomation.mcp"
live.BUILTIN_MODULES["com.inductiveautomation.historian"] = "Historian-module.modl"


def stage_mcp_module() -> bool:
    """Stage the MCP Module build ignition-mcp pins. Returns whether anything was staged."""
    if MCP_MODULE in {m["id"] for m in igdev("module", "list").get("private", [])}:
        return False
    modl = next((IGNITION_MCP / "tests" / "fixtures" / "modules").glob("*.modl"))
    igdev("module", "add", str(modl))
    return True


def grant_designer(gateway: Gateway) -> None:
    """Tick the disposable token's security level under Designer in Security > General
    Settings, which ignition-mcp's setup requires of its setup key. The owner approved this for
    the local igdev test gateway only (2026-09-30); `bootstrap_token` already grants access,
    read and write."""
    path = "/data/api/v1/resources/singleton/ignition/security-properties"
    status, props = gateway.request("GET", path)
    if status != 200:
        raise GatewayError(f"read security properties: {status}")
    config = props["config"]
    config["designerPermissions"] = config["writePermissions"]
    gateway.update(
        "ignition/security-properties",
        {"collection": props["collection"], "signature": props["signature"], "config": config},
    )


def create_historian(gateway: Gateway) -> None:
    kind = "com.inductiveautomation.historian/historian-provider"
    if gateway.find(kind, HISTORIAN)[0] == 200:
        return
    gateway.create(
        kind,
        {
            "name": HISTORIAN,
            "description": "History of the simulator's tags",
            "enabled": True,
            "config": {"profile": {"type": "CoreHistorian"}, "settings": {}},
        },
    )


def install_mcp(gateway: Gateway, work: Path) -> dict[str, str]:
    """Run ignition-mcp's setup CLI for the analysis role and return the endpoint and token."""
    deployment = Path.home() / ".config" / "ignition-mcp" / "deployments" / DEPLOYMENT
    shutil.rmtree(deployment, ignore_errors=True)  # this harness's own, for the previous gateway
    key = work / "gateway.token"
    key.write_text(gateway.token or "")
    key.chmod(0o600)
    done = subprocess.run(
        [
            "uv",
            "run",
            "--no-sync",
            "ignition-mcp",
            "setup",
            "--deployment",
            DEPLOYMENT,
            "--gateway-url",
            gateway.url,
            "--environment",
            "dev",
            "--roles",
            ROLE,
            "--gateway-token-file",
            str(key),
            "--yes",
            "--accept-certificate",
            "--accept-eula",
            "--json",
        ],
        cwd=IGNITION_MCP,
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    key.unlink()
    if done.returncode != 0:
        raise GatewayError(f"ignition-mcp setup: {done.stdout[-1500:]} {done.stderr[-800:]}")
    token = (deployment / f"runtime-{ROLE}.secret").read_text().strip()
    return {"url": f"{gateway.url}/data/mcp/{ROLE}", "token": token}


def stop_previous() -> None:
    """The previous run left its simulator serving the paused session; stop it."""
    try:
        pid = json.loads((AGENTS / "run.json").read_text())["simulator"]["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def _wall() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def run(scenario_id: str, baseline_s: float) -> dict[str, Any]:
    scenarios = ScenarioSet.read(ROOT / "data" / "scenarios" / "incidents.json")
    scenario = scenarios.get(scenario_id)
    doc = build(Sources.read(ROOT / "data" / "graphene"))
    work = Path(tempfile.mkdtemp(prefix="gws-agents-"))
    stop_previous()
    restage = live.stage_builtin_modules() | stage_mcp_module()
    gateway = Gateway.up()
    if restage:
        igdev("gateway", "restart")
        gateway.wait_running()
    gateway.bootstrap_token()
    AGENTS.mkdir(exist_ok=True)
    admin = AGENTS / "gateway.json"  # the disposable token, for inspecting this run by hand
    admin.write_text(json.dumps({"url": gateway.url, "token": gateway.token}))
    admin.chmod(0o600)
    grant_designer(gateway)
    create_historian(gateway)
    sim = live.Simulator.start(work)
    session = sim.call(
        "POST",
        "/api/runtime/sessions",
        {"scope": sorted(scope(doc)), "dt": scenarios.dt, "seed": 0},
    )
    sid = session["id"]
    for kind, payload in SETUP:
        assert kind == "conditions"
        sim.call("PUT", f"/api/runtime/sessions/{sid}/conditions", payload["changes"])
    sim.call(
        "POST", f"/api/runtime/sessions/{sid}/step", {"steps": scenarios.steps(scenarios.warmup_s)}
    )
    frame = sim.call("GET", f"/api/runtime/sessions/{sid}/frame")["points"]
    # Points the scope does not simulate read not_simulated; Ignition gets only the live ones.
    points = {p for p, v in frame.items() if v["quality"] == "good"}
    published = doc.model_copy(
        update={"point_bindings": {p: b for p, b in doc.point_bindings.items() if p in points}}
    )
    sim.call("PUT", "/api/opcua/session", {"session": sid})
    sim.call("POST", f"/api/runtime/sessions/{sid}/run", {"speed": 1.0})
    live.configure(gateway, sim.opc)
    _wait("OPC UA connection", 180, lambda: live.connection_healthy(gateway), 3)
    gateway.import_tags(live.PROVIDER, generate(published, live.CONNECTION, history=HISTORIAN))
    probe = live.Probe(gateway)

    def all_good() -> dict[str, Any] | None:
        s = probe.survey()
        return s if s["tags"] and s["qualities"].get("Good") == s["tags"] else None

    survey = _wait("every tag Good", 600, all_good, 10)
    (AGENTS / "mcp.json").write_text(json.dumps(install_mcp(gateway, work)))
    record: dict[str, Any] = {
        "scenario": scenario.id,
        "tags": survey["tags"],
        "session": sid,
        "simulator": {"url": f"http://127.0.0.1:{sim.http}", "pid": sim.process.pid},
        "baseline_start": _wall(),
    }
    t0 = time.monotonic() + baseline_s
    events = []
    for e in sorted(scenario.events, key=lambda e: e.at_s):
        time.sleep(max(0.0, t0 + e.at_s - time.monotonic()))
        if e.kind == "fault":
            sim.call("POST", f"/api/runtime/sessions/{sid}/faults", e.payload)
        elif e.kind == "conditions":
            sim.call("PUT", f"/api/runtime/sessions/{sid}/conditions", e.payload["changes"])
        else:
            sim.call("POST", f"/api/runtime/sessions/{sid}/commands", e.payload)
        events.append({"at": _wall(), "kind": e.kind, "payload": e.payload})
    time.sleep(max(0.0, t0 + scenario.observe_s - time.monotonic()))
    sim.call("POST", f"/api/runtime/sessions/{sid}/pause")
    record |= {"events": events, "end": _wall()}
    (AGENTS / "run.json").write_text(json.dumps(record, indent=2))
    (AGENTS / "brief.md").write_text(brief(record) + "\n")
    return record


def brief(record: dict[str, Any]) -> str:
    """The analyst's task for this run: `analyst.md` below its rule, filled in."""
    text = (Path(__file__).with_name("analyst.md")).read_text().split("\n---\n", 1)[1]
    start, end = (datetime.fromisoformat(record[k]) for k in ("baseline_start", "end"))
    return (
        text.replace("{IGNITION_MCP}", str(IGNITION_MCP))
        .replace("{ROOT}", str(ROOT))
        .replace("{WINDOW_START}", f"{start:%H:%M} on {start:%Y-%m-%d}")
        .replace("{WINDOW_END}", f"{end:%H:%M} on {end:%Y-%m-%d}")
        .strip()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scenario")
    parser.add_argument("--baseline-s", type=float, default=300.0)
    args = parser.parse_args()
    print(json.dumps(run(args.scenario, args.baseline_s), indent=2))


if __name__ == "__main__":
    main()

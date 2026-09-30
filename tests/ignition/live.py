"""Ignition integration test (#49): a real gateway reads, writes and alarms on the simulator.

    uv run python tests/ignition/live.py [--evidence docs/reports/phase4-ignition.json]

It needs Docker, igdev on PATH (or `IGDEV`), the Ignition EULA accepted on this machine
(`igdev setup --accept-eula`, a human's decision), and the Phase 1 slice FMU (cached in
`GWS_FMU_CACHE`, or OpenModelica to compile it). It:

1. starts the checkout's igdev gateway with the OPC UA and WebDev modules, and installs a
   disposable API token;
2. starts the simulator (`gws_api.serve`) with the Graphene World Model, runs the slice and
   serves it over OPC UA;
3. creates the gateway's `Graphene Demo Twin` OPC UA connection to it and a `DemoTwin` tag
   provider, imports the tags `gws_api.ignition` generates, and imports a WebDev probe
   project for reads, writes and alarm queries;
4. checks that Ignition reads the simulator's values, that an Ignition write to a command
   point stops the chiller through the runtime's event log, that a write to a measured point
   is refused, and that a chiller trip raises the generated alarm.

Exit status 0 means every check passed. The gateway keeps running afterwards
(`igdev gateway down --volumes` removes it).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gateway import ROOT, Gateway, GatewayError, _wait, igdev  # noqa: E402

from gws_api.ignition import generate  # noqa: E402
from gws_runtime.gate import IT_FRACTION, SLICE  # noqa: E402
from gws_world_model.importers.graphene import Sources, build  # noqa: E402

PROVIDER = "DemoTwin"
CONNECTION = "Graphene Demo Twin"
PROJECT = "gws_probe"
BUILTIN_MODULES = {
    "com.inductiveautomation.opcua": "OPC-UA-module.modl",
    "com.inductiveautomation.webdev": "Web Developer Module.modl",
}
C1 = "Chiller/R_C1"
CH = "Chiller System Control/Chillers/CH-001"
POWER, ON_OFF, MODE, TRIP = (
    f"{C1}/Input Power",
    f"{C1}/On_Off",
    f"{C1}/Auto_Manual",
    f"{C1}/System Failure_Trip",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("0.0.0.0", 0))
        port: int = s.getsockname()[1]
        return port


def stage_builtin_modules() -> bool:
    """igdev mounts its staged modules over the image's module folder, so the built-in modules
    the test needs are staged from the image. Returns whether anything was staged."""
    modules = igdev("module", "list")
    staged = {m["id"] for m in modules.get("private", [])}
    missing = {m: f for m, f in BUILTIN_MODULES.items() if m not in staged}
    if not missing:
        return False
    image = f"inductiveautomation/ignition:{modules['ignition_version']}"
    work = Path(tempfile.mkdtemp(prefix="gws-modl-"))
    container = subprocess.run(
        ["docker", "create", image], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        for name in missing.values():
            target = work / name.replace(" ", "-")
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{container}:/usr/local/bin/ignition/user-lib/modules/{name}",
                    str(target),
                ],
                check=True,
            )
            igdev("module", "add", str(target))
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, check=False)
    return True


@dataclass
class Simulator:
    http: int
    opc: int
    process: subprocess.Popen[bytes]
    log: Path

    @classmethod
    def start(cls, work: Path) -> Simulator:
        http, opc = _free_port(), _free_port()
        log = work / "serve.log"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "gws_api.serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(http),
                "--opc-endpoint",
                f"opc.tcp://0.0.0.0:{opc}/graphene/twin",
                "--db",
                str(work / "world.sqlite"),
                "--import-graphene",
                str(ROOT / "data" / "graphene"),
            ],
            cwd=ROOT,
            stdout=log.open("wb"),
            stderr=subprocess.STDOUT,
        )
        sim = cls(http, opc, process, log)
        _wait("simulator API", 120, lambda: sim.call("GET", "/api/opcua", ok=(200, 0)), 1)
        return sim

    def call(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = ()) -> Any:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.http}{path}",
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                data = r.read()
        except OSError:
            if 0 in ok:
                return None
            raise
        return json.loads(data) if data else {}

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(30)


@dataclass
class Report:
    checks: list[dict[str, Any]] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    def check(self, name: str, passed: bool, detail: Any) -> None:
        self.checks.append({"name": name, "passed": bool(passed), "detail": detail})
        print(f"{'PASS' if passed else 'FAIL'} {name}: {detail}", flush=True)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c["passed"] for c in self.checks)


class Probe:
    """The WebDev probe project: tag reads, writes and alarm queries inside the gateway."""

    def __init__(self, gateway: Gateway) -> None:
        self.base = f"{gateway.url}/system/webdev/{PROJECT}/probe"

    def _get(self, **params: str) -> Any:
        url = self.base + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())

    def read(self, path: str) -> dict[str, Any]:
        result: dict[str, Any] = self._get(op="read", path=f"[{PROVIDER}]{path}")
        return result

    def alarms(self, source: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._get(op="alarms", source=source)
        return result

    def write(self, path: str, value: Any) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base,
            data=json.dumps({"path": f"[{PROVIDER}]{path}", "value": value}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            result: dict[str, Any] = json.loads(r.read())
            return result


def configure(gateway: Gateway, opc_port: int) -> None:
    """The OPC UA connection (settings copied from the gateway's loopback connection, so every
    field the connection type expects is present), the tag provider and the probe project."""
    status, listed = gateway.request("GET", "/data/api/v1/resources/list/ignition/opc-connection")
    if status != 200:
        raise GatewayError(f"list OPC connections: {status}")
    items = {item["name"]: item for item in listed["items"]}
    settings = copy.deepcopy(items["Ignition OPC UA Server"]["config"]["settings"])
    url = f"opc.tcp://{gateway.network_gateway}:{opc_port}/graphene/twin"
    settings["endpoint"] = {
        "discoveryUrl": url,
        "endpointUrl": url,
        "securityPolicy": "None",
        "securityMode": "None",
        "hostOverride": gateway.network_gateway,
    }
    settings["authentication"] = {"authenticationType": "ANONYMOUS", "username": ""}
    settings["security"]["certificateValidationEnabled"] = False
    resource: dict[str, Any] = {
        "name": CONNECTION,
        "description": "The graphene-world-sim simulator",
        "enabled": True,
        "config": {
            "profile": {"type": "com.inductiveautomation.OpcUaServerType", "readOnly": False},
            "settings": settings,
        },
    }
    if CONNECTION in items:
        resource["signature"] = items[CONNECTION]["signature"]
        gateway.update("ignition/opc-connection", resource)
    else:
        gateway.create("ignition/opc-connection", resource)
    if gateway.find("ignition/tag-provider", PROVIDER)[0] != 200:
        gateway.create(
            "ignition/tag-provider",
            {
                "name": PROVIDER,
                "description": "Tags generated from the World Model's point bindings",
                "enabled": True,
                "config": {"profile": {"type": "STANDARD"}, "settings": {}},
            },
        )
    _wait(
        "tag provider",
        120,
        lambda: gateway.request(
            "GET", "/data/api/v1/tags/export", query={"provider": PROVIDER, "type": "json"}
        )[0]
        == 200,
    )
    gateway.import_project(PROJECT, Path(__file__).resolve().parent / "project")


def connection_healthy(gateway: Gateway) -> bool:
    status, body = gateway.request("GET", "/data/api/v1/resources/list/ignition/opc-connection")
    if status != 200:
        return False
    for item in body.get("items", []):
        if item["name"] == CONNECTION:
            return bool(
                item.get("healthchecks", {}).get("status", {}).get("result", {}).get("healthy")
            )
    return False


def run(evidence: Path | None) -> Report:
    report = Report()
    work = Path(tempfile.mkdtemp(prefix="gws-ignition-"))
    restage = stage_builtin_modules()
    gateway = Gateway.up()
    if restage:
        igdev("gateway", "restart")
        gateway.wait_running()
    gateway.bootstrap_token()
    _, info = gateway.request("GET", "/data/api/v1/gateway-info")
    report.facts["gateway"] = (
        {k: info.get(k) for k in ("ignitionVersion", "edition")} if isinstance(info, dict) else info
    )
    sim = Simulator.start(work)
    try:
        doc = build(Sources.read(ROOT / "data" / "graphene"))
        document = generate(doc, CONNECTION)
        report.facts["generated"] = {
            "points": len(doc.point_bindings),
            "udts": len(next(t for t in document["tags"] if t["name"] == "_types_")["tags"]),
        }

        session = sim.call("POST", "/api/runtime/sessions", {"scope": list(SLICE), "dt": 5.0})
        sid = session["id"]
        sim.call(
            "PUT", f"/api/runtime/sessions/{sid}/conditions", {"it_fraction": {"DH01": IT_FRACTION}}
        )
        sim.call("POST", f"/api/runtime/sessions/{sid}/step", {"steps": 12})
        sim.call("PUT", "/api/opcua/session", {"session": sid})
        sim.call("POST", f"/api/runtime/sessions/{sid}/run", {"speed": 5.0})

        configure(gateway, sim.opc)
        _wait("OPC UA connection", 180, lambda: connection_healthy(gateway), 3)
        report.check("OPC UA connection to the simulator is healthy", True, CONNECTION)
        started = time.monotonic()
        result = gateway.import_tags(PROVIDER, document)
        report.facts["import"] = {
            "seconds": round(time.monotonic() - started, 1),
            "result": str(result)[:300],
        }
        probe = Probe(gateway)

        power = _wait(
            "a good chiller power reading", 240, lambda: (r := probe.read(POWER))["good"] and r, 3
        )
        frame = sim.call("GET", f"/api/runtime/sessions/{sid}/frame")
        simulated = frame["points"][POWER]["value"]
        report.check(
            "Ignition reads the simulator (UDT member)",
            abs(power["value"] - simulated) <= max(0.1 * abs(simulated), 1.0),
            {"ignition": power["value"], "simulator": simulated, "quality": power["quality"]},
        )
        loose = probe.read("Environment Monitoring/Level 1/DH01/IT Load")
        report.check("Ignition reads the simulator (plain tag)", loose["good"], loose)
        outside = probe.read("Chiller/R_C2/Input Power")
        report.check(
            "a point outside the session's scope is bad",
            not outside["good"] and "OutOfService" in outside["quality"],
            outside["quality"],
        )

        refused = probe.write(POWER, 1.0)
        report.check("a write to a measured point is refused", not refused["good"], refused)
        written = probe.write(f"{CH}/Commands/Stop", True)
        events = sim.call("GET", f"/api/runtime/sessions/{sid}/events")
        commands = [e["payload"] for e in events if e["kind"] == "command"]
        report.check(
            "an Ignition write is a runtime command in the event log",
            written["good"]
            and {"target": f"{CH}/Commands/Stop", "signal": None, "value": True} in commands,
            {"write": written, "commands": commands},
        )
        stopped = _wait(
            "the chiller to stop",
            120,
            lambda: (r := probe.read(ON_OFF))["good"] and r["value"] == 0 and r,
            3,
        )
        mode = probe.read(MODE)
        report.check(
            "the chiller stops and reads manual in Ignition",
            stopped["value"] == 0 and mode["value"] == 0,
            {"On_Off": stopped["value"], "Auto_Manual": mode["value"]},
        )

        probe.write(f"{CH}/Commands/Start", True)
        _wait("the chiller to restart", 120, lambda: probe.read(ON_OFF)["value"] == 1, 3)
        fault = sim.call(
            "POST", f"/api/runtime/sessions/{sid}/faults", {"target": C1, "mode": "trip"}
        )

        def active() -> list[dict[str, Any]]:
            found = probe.alarms(f"*{TRIP}*")
            return [a for a in found if "Active" in a["state"]]

        alarms = _wait("the trip alarm", 120, active, 3)
        report.check(
            "a chiller trip raises the generated alarm in Ignition",
            bool(alarms),
            {"fault": fault["id"], "alarms": alarms},
        )
        sim.call("POST", f"/api/runtime/sessions/{sid}/pause")
    except (GatewayError, OSError, KeyError) as e:
        report.check("the run completed", False, f"{type(e).__name__}: {e}")
    finally:
        sim.stop()
        tail = sim.log.read_text(errors="replace")[-2000:]
        if not report.passed:
            print(tail)
    if evidence is not None:
        evidence.write_text(
            json.dumps(
                {"passed": report.passed, "facts": report.facts, "checks": report.checks},
                indent=2,
                default=str,
            )
            + "\n"
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--evidence", type=Path, help="write the results as JSON here")
    args = parser.parse_args()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    report = run(args.evidence)
    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()

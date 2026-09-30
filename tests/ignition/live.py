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
from gws_world_model.model import WorldModel  # noqa: E402

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

    def survey(self) -> dict[str, Any]:
        """Every atomic tag the provider holds (not the UDT definitions) and their qualities."""
        result: dict[str, Any] = self._get(op="survey", provider=PROVIDER)
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


SITE_RUN_S = 600.0
"""How long the site gate watches the whole site run at real time."""


def run_site(evidence: Path | None) -> Report:
    """The Phase 6 gate: the whole site at 1x real time, every [DemoTwin] tag Good."""
    report = Report()
    work = Path(tempfile.mkdtemp(prefix="gws-ignition-site-"))
    restage = stage_builtin_modules()
    gateway = Gateway.up()
    if restage:
        igdev("gateway", "restart")
        gateway.wait_running()
    gateway.bootstrap_token()
    sim = Simulator.start(work)
    try:
        doc = build(Sources.read(ROOT / "data" / "graphene"))
        document = generate(doc, CONNECTION)
        report.facts["generated"] = {"points": len(doc.point_bindings)}
        session = sim.call(
            "POST", "/api/runtime/sessions", {"scope": sorted(doc.assets), "dt": 1.0}
        )
        sid = session["id"]
        sim.call("POST", f"/api/runtime/sessions/{sid}/step", {"steps": 60})
        sim.call("PUT", "/api/opcua/session", {"session": sid})
        sim.call("POST", f"/api/runtime/sessions/{sid}/run", {"speed": 1.0})
        configure(gateway, sim.opc)
        _wait("OPC UA connection", 180, lambda: connection_healthy(gateway), 3)
        gateway.import_tags(PROVIDER, document)
        probe = Probe(gateway)

        def all_good() -> dict[str, Any] | None:
            s = probe.survey()
            return s if s["tags"] and s["qualities"].get("Good") == s["tags"] else None

        t0, wall0 = sim.call("GET", f"/api/runtime/sessions/{sid}")["t"], time.monotonic()
        try:
            survey = _wait("every DemoTwin tag Good", 600, all_good, 15)
        except GatewayError:
            survey = probe.survey()
        report.check(
            "every [DemoTwin] tag reads Good in Ignition",
            survey["qualities"].get("Good") == survey["tags"] == len(doc.point_bindings),
            {"tags": survey["tags"], "qualities": survey["qualities"], "bad": survey["bad"][:10]},
        )
        remaining = SITE_RUN_S - (time.monotonic() - wall0)
        if remaining > 0:
            time.sleep(remaining)  # the site keeps running at real time meanwhile
        t1, wall1 = sim.call("GET", f"/api/runtime/sessions/{sid}")["t"], time.monotonic()
        rtf = (t1 - t0) / (wall1 - wall0)
        report.check(
            "the whole site keeps real time (1 s step)",
            rtf >= 0.98,
            {"simulated_s": round(t1 - t0, 1), "wall_s": round(wall1 - wall0, 1), "rtf": rtf},
        )
        final = probe.survey()
        report.check(
            "every tag is still Good after the run",
            final["qualities"].get("Good") == final["tags"],
            {"qualities": final["qualities"], "bad": final["bad"][:10]},
        )
        sim.call("POST", f"/api/runtime/sessions/{sid}/pause")
    except (GatewayError, OSError, KeyError) as e:
        report.check("the run completed", False, f"{type(e).__name__}: {e}")
    finally:
        sim.stop()
        if not report.passed:
            print(sim.log.read_text(errors="replace")[-2000:])
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


NEW_CHILLER = "Chiller/R_C9"
SWAP_TIMEOUT_S = 1800
"""Adding a chiller recompiles its partition, which takes minutes."""


def branch(document: dict[str, Any], path: str) -> dict[str, Any]:
    """The tag import document cut down to the UDT definitions and the tags at `path`, so a
    re-import adds a new asset without rewriting every other tag."""

    def keep(tags: list[dict[str, Any]], segments: list[str]) -> list[dict[str, Any]]:
        for tag in tags:
            if tag["name"] != segments[0]:
                continue
            if len(segments) == 1:
                return [tag]
            inner = keep(tag.get("tags", []), segments[1:])
            return [tag | {"tags": inner}] if inner else []
        return []

    types = [t for t in document["tags"] if t["name"] == "_types_"]
    return {"tags": types + keep(document["tags"], path.split("/"))}


def add_chiller_ops(doc: Any) -> list[dict[str, Any]]:
    """Draft operations that place a second chiller beside R_C1, piped and fed like it."""
    c1 = doc.assets["Chiller/R_C1"].model_dump(mode="json")
    ops: list[dict[str, Any]] = [{"op": "place", "value": c1 | {"id": NEW_CHILLER, "name": "R_C9"}}]
    for c in doc.connections.values():
        if "Chiller/R_C1" not in (c.source.node, c.target.node):
            continue
        value = c.model_dump(mode="json")
        for end in ("source", "target"):
            if value[end]["node"] == "Chiller/R_C1":
                value[end]["node"] = NEW_CHILLER
        value["id"] = c.id.replace("Chiller/R_C1", NEW_CHILLER)
        ops.append({"op": "put", "collection": "connections", "value": value})
    return ops


def run_reconfigure(evidence: Path | None) -> Report:
    """The Phase 7 gate: a chiller added to the running World Model reaches Ignition tags
    without restarting anything; removing it takes its tags' source away."""
    report = Report()
    work = Path(tempfile.mkdtemp(prefix="gws-ignition-reconf-"))
    restage = stage_builtin_modules()
    gateway = Gateway.up()
    if restage:
        igdev("gateway", "restart")
        gateway.wait_running()
    gateway.bootstrap_token()
    sim = Simulator.start(work)
    try:
        doc = build(Sources.read(ROOT / "data" / "graphene"))
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
        gateway.import_tags(PROVIDER, generate(doc, CONNECTION))
        probe = Probe(gateway)
        _wait("a good chiller power reading", 240, lambda: probe.read(POWER)["good"], 3)
        pid = sim.process.pid

        # Add a chiller in the World Model, as the Engineering workspace does, and apply it to
        # the running session.
        draft = sim.call("POST", "/api/world-model/drafts", {"author": "live-test"})
        sim.call("POST", f"/api/world-model/drafts/{draft['id']}/operations", add_chiller_ops(doc))
        revision = sim.call(
            "POST",
            f"/api/world-model/drafts/{draft['id']}/apply",
            {"message": "add chiller R_C9", "author": "live-test"},
        )["number"]
        before = sim.call("GET", f"/api/runtime/sessions/{sid}")
        swap = sim.call("POST", f"/api/runtime/sessions/{sid}/swap", {"revision": revision})
        report.facts["swap_requested"] = swap
        started = time.monotonic()
        during = _wait(
            "the session steps while the chiller compiles",
            300,
            lambda: (i := sim.call("GET", f"/api/runtime/sessions/{sid}"))["t"] > before["t"] and i,
            3,
        )
        kept = probe.read(POWER)
        report.check(
            "the session keeps running while the new partition compiles",
            during["swap"]["state"] in ("compiling", "ready", "applied") and kept["good"],
            {"t_before": before["t"], "t_during": during["t"], "swap": during["swap"]["state"]},
        )
        applied = _wait(
            "the swap",
            SWAP_TIMEOUT_S,
            lambda: (w := sim.call("GET", f"/api/runtime/sessions/{sid}/swap"))["state"]
            in ("applied", "failed")
            and w,
            5,
        )
        report.facts["swap"] = applied | {"wall_s": round(time.monotonic() - started, 1)}
        report.check(
            "the new chiller swaps into the running session",
            applied["state"] == "applied" and NEW_CHILLER in applied["added"],
            applied,
        )
        grown = sim.call("GET", f"/api/world-model/revisions/{revision}/document")
        new_points = sorted(p for p in grown["point_bindings"] if p.startswith(NEW_CHILLER + "/"))
        report.facts["new_points"] = len(new_points)
        document = branch(generate(WorldModel.model_validate(grown), CONNECTION), NEW_CHILLER)
        gateway.import_tags(PROVIDER, document)
        new_power = f"{NEW_CHILLER}/Input Power"
        reading = _wait(
            "a good reading of the new chiller",
            240,
            lambda: (r := probe.read(new_power))["good"] and r,
            3,
        )
        frame = sim.call("GET", f"/api/runtime/sessions/{sid}/frame")
        report.check(
            "Ignition reads the new chiller's tags",
            reading["good"] and new_power in frame["points"],
            {"ignition": reading, "simulator": frame["points"].get(new_power)},
        )
        report.check(
            "nothing was restarted",
            sim.process.pid == pid
            and sim.process.poll() is None
            and connection_healthy(gateway)
            and probe.read(POWER)["good"],
            {"simulator_pid": pid, "session": sid},
        )

        # Remove it again: its points read Bad, then their nodes go.
        draft = sim.call("POST", "/api/world-model/drafts", {"author": "live-test"})
        sim.call(
            "POST",
            f"/api/world-model/drafts/{draft['id']}/operations",
            [{"op": "remove", "key": NEW_CHILLER}],
        )
        revision = sim.call(
            "POST",
            f"/api/world-model/drafts/{draft['id']}/apply",
            {"message": "remove chiller R_C9", "author": "live-test"},
        )["number"]
        sim.call("POST", f"/api/runtime/sessions/{sid}/swap", {"revision": revision})

        def not_good() -> dict[str, Any] | None:
            r = probe.read(new_power)
            return None if r["good"] else r

        bad = _wait("the removed chiller reads Bad", 120, not_good, 1)
        points = sim.call("GET", "/api/opcua")["points"]
        gone = _wait(
            "the removed chiller's nodes go",
            120,
            lambda: (n := sim.call("GET", "/api/opcua")["points"]) < points and n,
            2,
        )
        after = probe.read(new_power)
        report.check(
            "a removed asset reads Bad in Ignition, then its source disappears",
            not bad["good"] and not after["good"] and gone == points - len(new_points),
            {"while_removing": bad, "after": after, "points": [points, gone]},
        )
        sim.call("POST", f"/api/runtime/sessions/{sid}/pause")
    except (GatewayError, OSError, KeyError) as e:
        report.check("the run completed", False, f"{type(e).__name__}: {e}")
    finally:
        sim.stop()
        if not report.passed:
            print(sim.log.read_text(errors="replace")[-3000:])
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
    parser.add_argument(
        "--site", action="store_true", help="the Phase 6 gate: the whole site at real time"
    )
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="the Phase 7 gate: add a chiller to the running session and read it in Ignition",
    )
    args = parser.parse_args()
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    if args.site:
        report = run_site(args.evidence)
    elif args.reconfigure:
        report = run_reconfigure(args.evidence)
    else:
        report = run(args.evidence)
    sys.exit(0 if report.passed else 1)


if __name__ == "__main__":
    main()

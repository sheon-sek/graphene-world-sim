"""Model compiler: World Model -> partitioned Modelica models -> cached FMUs.

For a scope of assets, the compiler:

1. keeps the assets whose ComponentType has a thermofluid behaviour (`behaviours.BEHAVIOURS`)
   and the fluid connections between them; everything else is left to other domains or is
   reported as not modelled;
2. closes every water loop: the World Model draws supply paths, so every outlet with no
   onward connection drains into the loop's return header, which feeds every inlet with no
   supply. The header carries the loop's pressure reference (an expansion vessel);
3. gives every room that a unit supplies air to an air volume model (`GwsLib.Hall`); a unit's
   open air inlet takes return air from the room its outlet serves;
4. splits the result into partitions: one per group of assets that exchange fluid or heat
   directly. Partitions couple to each other and to the other domains only through signals
   the master exchanges every step (supply voltage, power, commands, conditions);
5. writes one Modelica model per partition, named by the hash of its content, and compiles it
   to an FMU with OpenModelica in a container, reusing the cached FMU when the hash matches.

Generated sources are deterministic: the same World Model and scope give byte-identical
models, so an unchanged partition is never recompiled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gws_runtime.behaviours import BEHAVIOURS, HALL, Behaviour, Params, modelica_real, si
from gws_world_model.model import ROOM_PREFIX, Asset, Domain, WorldModel

GWSLIB = Path(__file__).with_name("modelica") / "GwsLib" / "package.mo"
TOOLCHAIN = "openmodelica-1.25.0+msl-4.0.0+buildings-11.1.0"
"""Part of every partition hash: a different compiler or library must rebuild."""
FLUID = frozenset({Domain.CHW, Domain.CW, Domain.WATER, Domain.AIR})
HEADER_VOLUME_M3 = 1.0


class CompileError(ValueError):
    """The scope cannot be turned into a valid model (listed, one problem per line)."""


def ident(asset_id: str) -> str:
    """Modelica identifier for an asset id: `Chiller/R_C1` -> `Chiller_R_C1`."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", asset_id)
    return name if name[:1].isalpha() else f"a_{name}"


def room_key(room: str) -> str:
    """How a room's air volume appears in true state: like an asset id, `room:DH01`."""
    return f"{ROOM_PREFIX}{room}"


@dataclass(frozen=True, slots=True)
class Variable:
    """A top-level FMU input or output and what it means."""

    name: str
    asset: str | None
    """Asset id, `room:<id>`, or None for an operating condition."""
    signal: str
    kind: str
    unit: str


@dataclass(frozen=True, slots=True)
class Partition:
    name: str
    """Modelica model name: `P_` and the first 16 hex digits of the content hash."""
    content_hash: str
    source: str
    assets: tuple[str, ...]
    rooms: tuple[str, ...]
    inputs: tuple[Variable, ...]
    outputs: tuple[Variable, ...]
    state: dict[str, dict[str, str]]
    """Asset (or `room:<id>`) -> start parameter -> FMU variable to read it from."""
    start: dict[str, dict[str, str]]
    """Asset (or `room:<id>`) -> start parameter -> FMU parameter to write it to."""
    power: dict[str, str]
    """Asset -> FMU output carrying its electrical demand in W."""

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "content_hash": self.content_hash,
            "assets": list(self.assets),
            "rooms": list(self.rooms),
            "inputs": [_var(v) for v in self.inputs],
            "outputs": [_var(v) for v in self.outputs],
        }


def _var(v: Variable) -> dict[str, object]:
    return {"name": v.name, "asset": v.asset, "signal": v.signal, "kind": v.kind, "unit": v.unit}


@dataclass(frozen=True, slots=True)
class Plan:
    partitions: tuple[Partition, ...]
    modelled: frozenset[str]
    """Assets with a thermofluid model in some partition."""
    not_modelled: dict[str, str]
    """Assets in scope with no thermofluid model, and why."""
    dropped: tuple[str, ...]
    """Fluid connections left out because an end is outside the modelled set."""


@dataclass
class _Unit:
    asset: Asset
    behaviour: Behaviour
    params: Params
    name: str


@dataclass
class _Build:
    units: dict[str, _Unit] = field(default_factory=dict)
    rooms: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)
    """Room -> (asset, Modelica port, 'supply'|'return') air links, in a fixed order."""
    links: list[tuple[str, str, str, str]] = field(default_factory=list)
    """(asset, Modelica port, asset, Modelica port) fluid connections between units."""
    problems: list[str] = field(default_factory=list)


def _params(doc: WorldModel, asset: Asset) -> Params:
    ctype = doc.component_types[asset.type]
    return {name: spec.default for name, spec in ctype.parameters.items()} | dict(asset.parameters)


def _collect(doc: WorldModel, scope: Collection[str]) -> tuple[_Build, dict[str, str], list[str]]:
    b = _Build()
    not_modelled: dict[str, str] = {}
    for asset_id in sorted(scope):
        asset = doc.assets.get(asset_id)
        if asset is None:
            b.problems.append(f"scope names unknown asset {asset_id!r}")
            continue
        behaviour_name = doc.component_types[asset.type].behaviour
        behaviour = BEHAVIOURS.get(behaviour_name or "")
        if behaviour is None:
            not_modelled[asset_id] = (
                f"{asset.type} has no thermofluid behaviour"
                if behaviour_name is None
                else f"behaviour {behaviour_name} is not a thermofluid model"
            )
            continue
        b.units[asset_id] = _Unit(asset, behaviour, _params(doc, asset), ident(asset_id))
    dropped: list[str] = []
    for cid in sorted(doc.connections):
        c = doc.connections[cid]
        if c.domain not in FLUID:
            continue
        src, dst = c.source, c.target
        if src.node not in b.units:
            if src.node in scope or dst.node in b.units:
                dropped.append(cid)
            continue
        src_port = b.units[src.node].behaviour.ports.get(src.port)
        if src_port is None:
            b.problems.append(f"{cid}: {src.node} port {src.port} has no fluid port in its model")
            continue
        if dst.is_room:
            if c.domain is not Domain.AIR:
                b.problems.append(f"{cid}: only air can be supplied to a room")
                continue
            b.rooms.setdefault(dst.room, []).append((src.node, src_port, "supply"))
            continue
        if dst.node not in b.units:
            if dst.node in scope or src.node in b.units:
                dropped.append(cid)
            continue
        dst_port = b.units[dst.node].behaviour.ports.get(dst.port)
        if dst_port is None:
            b.problems.append(f"{cid}: {dst.node} port {dst.port} has no fluid port in its model")
            continue
        b.links.append((src.node, src_port, dst.node, dst_port))
    return b, not_modelled, dropped


class _Components:
    """Union-find over nodes named `<asset>#<port>` and `room:<id>`."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def groups(self) -> list[list[str]]:
        out: dict[str, list[str]] = {}
        for node in sorted(self.parent):
            out.setdefault(self.find(node), []).append(node)
        return sorted(out.values())


@dataclass(frozen=True, slots=True)
class _Header:
    """How one open water loop is closed."""

    pumps: tuple[str, ...]
    outlets: list[tuple[str, str]]
    inlets: list[tuple[str, str]]
    m_flow_nominal: float


def _node(asset: str, port: str) -> str:
    return f"{asset}#{port}"


def plan(doc: WorldModel, scope: Collection[str]) -> Plan:
    """Partition the thermofluid part of a scope and generate each partition's model."""
    b, not_modelled, dropped = _collect(doc, scope)

    # Air: a unit's open air inlet takes return air from the room its outlet serves.
    connected = {(a, p) for a, p, _, _ in b.links} | {(a, p) for _, _, a, p in b.links}
    for room, links in list(b.rooms.items()):
        for asset, _, _ in list(links):
            unit = b.units[asset]
            for inlet, outlet in unit.behaviour.passages:
                if unit.behaviour.medium.get(inlet) == "air" and (asset, inlet) not in connected:
                    if (asset, outlet, "supply") in links:
                        links.append((asset, inlet, "return"))
        b.rooms[room] = sorted(set(links))
        if room not in doc.site.rooms:
            b.problems.append(f"air is supplied to unknown room {room!r}")

    # Loops: fluid passages, connections and rooms join ports into loops. A passage no
    # connection touches is unused (a pump's second domain) and is left out.
    linked = {(a, p) for a, p, _, _ in b.links} | {(c, p) for _, _, c, p in b.links}
    linked |= {(a, p) for links in b.rooms.values() for a, p, _ in links}
    loops = _loops(b, linked)

    headers: list[_Header] = []
    for group in loops.groups():
        if any(n.startswith(ROOM_PREFIX) for n in group):
            continue  # an air loop through a room: the room closes it
        ports = [tuple(n.split("#", 1)) for n in group if "#" in n]
        outlets: list[tuple[str, str]] = []
        inlets: list[tuple[str, str]] = []
        for asset, port in ports:
            unit = b.units[asset]
            if (asset, port) in linked:
                continue
            if unit.behaviour.medium.get(port) == "air":
                b.problems.append(f"{asset} air port {port} is not connected to anything")
                continue
            if any(port == inlet for inlet, _ in unit.behaviour.passages):
                inlets.append((asset, port))
            else:
                outlets.append((asset, port))
        pumps = sorted({a for a, _ in ports if b.units[a].behaviour.pump})
        if not pumps:
            b.problems.append(
                f"water loop through {', '.join(sorted({a for a, _ in ports}))} has no pump"
            )
            continue
        m_nominal = max(float(b.units[p].params.get("m_flow_nominal", 1.0)) for p in pumps)
        if bool(inlets) != bool(outlets):
            b.problems.append(
                f"water loop through {', '.join(pumps)} has open "
                + ("inlets" if inlets else "outlets")
                + " but nothing to close them with"
            )
            continue
        headers.append(_Header(tuple(pumps), sorted(outlets), sorted(inlets), m_nominal))

    if b.problems:
        raise CompileError("\n".join(sorted(set(b.problems))))

    # Partitions: everything that shares a loop, a room or a unit.
    parts = _Components()
    for asset in b.units:
        parts.find(asset)
    for group in loops.groups():
        owners = sorted({n.split("#", 1)[0] for n in group})
        for other in owners[1:]:
            parts.union(owners[0], other)
    partitions = []
    for group in parts.groups():
        members = [n for n in group if not n.startswith(ROOM_PREFIX)]
        rooms = sorted(n.removeprefix(ROOM_PREFIX) for n in group if n.startswith(ROOM_PREFIX))
        if not members:
            continue
        partitions.append(_generate(doc, b, members, rooms, headers))
    return Plan(
        partitions=tuple(partitions),
        modelled=frozenset(b.units),
        not_modelled=not_modelled,
        dropped=tuple(dropped),
    )


def _loops(b: _Build, linked: set[tuple[str, str]]) -> _Components:
    loops = _Components()
    for asset, unit in b.units.items():
        for inlet, outlet in unit.behaviour.passages:
            if (asset, inlet) in linked or (asset, outlet) in linked:
                loops.union(_node(asset, inlet), _node(asset, outlet))
    for a, pa, c, pc in b.links:
        loops.union(_node(a, pa), _node(c, pc))
    for room, links in b.rooms.items():
        for asset, port, _ in links:
            loops.union(_node(asset, port), room_key(room))
    return loops


def _literal(value: float | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return modelica_real(value)


def _generate(
    doc: WorldModel,
    b: _Build,
    members: list[str],
    rooms: list[str],
    headers: list[_Header],
) -> Partition:
    decls: list[str] = []
    eqs: list[str] = []
    inputs: list[Variable] = []
    outputs: list[Variable] = []
    state: dict[str, dict[str, str]] = {}
    start: dict[str, dict[str, str]] = {}
    power: dict[str, str] = {}
    environment: set[str] = set()
    member_set = set(members)

    def io(owner: str, inst: str, behaviour: Behaviour, values: dict[str, float | bool]) -> None:
        for sig, spec in behaviour.inputs.items():
            if spec.environment:
                environment.add(spec.environment)
                eqs.append(f"  {inst}.{sig} = env_{spec.environment};")
                continue
            var = f"{inst}_{sig}"
            kind = "Boolean" if spec.kind == "bool" else "Real"
            decls.append(f"  input {kind} {var}(start={_literal(values.get(sig, spec.default))});")
            eqs.append(f"  {inst}.{sig} = {var};")
            inputs.append(Variable(var, owner, sig, spec.kind, spec.unit))
        for sig, out in behaviour.outputs.items():
            var = f"{inst}_{sig}"
            kind = "Boolean" if out.kind == "bool" else "Real"
            decls.append(f"  output {kind} {var};")
            eqs.append(f"  {var} = {inst}.{sig};")
            outputs.append(Variable(var, owner, sig, out.kind, out.unit))
        state[owner] = {p: f"{inst}.{v}" for p, v in behaviour.state.items()}
        start[owner] = {p: f"{inst}.{p}" for p in behaviour.state}
        if behaviour.power:
            power[owner] = f"{inst}_{behaviour.power}"

    for asset in members:
        unit = b.units[asset]
        mods = ", ".join(f"{k}={v}" for k, v in unit.behaviour.modifiers(unit.params).items())
        decls.append(f'  {unit.behaviour.modelica} {unit.name}({mods}) "{asset}";')
        values: dict[str, float | bool] = {}
        for sig, spec in unit.behaviour.inputs.items():
            if spec.parameter and spec.parameter in unit.params:
                raw = unit.params[spec.parameter]
                unit_of = doc.component_types[unit.asset.type].parameters[spec.parameter].unit
                values[sig] = raw if isinstance(raw, bool) else si(float(raw), unit_of)
        io(asset, unit.name, unit.behaviour, values)

    for room in rooms:
        r = doc.site.rooms[room]
        floor = next(f for f in doc.site.floors if f.id == r.floor)
        links = b.rooms[room]
        inst = f"room_{ident(room)}"
        m_air = sum(
            float(b.units[a].params.get("m_air_flow_nominal", 1.0))
            for a, _, kind in links
            if kind == "supply"
        )
        decls.append(
            f"  GwsLib.Hall {inst}(nPorts={len(links)}, "
            f"V={modelica_real(r.w * r.h * floor.height_m)}, "
            f'mAir_flow_nominal={modelica_real(max(m_air, 0.1))}) "room {room}";'
        )
        io(room_key(room), inst, HALL, {})
        for i, (asset, port, _) in enumerate(links, start=1):
            eqs.append(f"  connect({b.units[asset].name}.{port}, {inst}.ports[{i}]);")

    for a, pa, c, pc in b.links:
        if a in member_set:
            eqs.append(f"  connect({b.units[a].name}.{pa}, {b.units[c].name}.{pc});")

    for n, h in enumerate(h for h in headers if h.pumps[0] in member_set):
        pump = h.pumps[0]
        why = f"of the loop through {pump}"
        if h.outlets:
            hdr = f"hdr{n}"
            ports = [*h.outlets, *h.inlets]
            decls.append(
                f"  GwsLib.ReturnHeader {hdr}(nPorts={len(ports) + 1}, "
                f"V={modelica_real(HEADER_VOLUME_M3)}, "
                f'm_flow_nominal={modelica_real(h.m_flow_nominal)}) "return header {why}";'
            )
            decls.append(f'  GwsLib.Expansion exp{n} "pressure reference {why}";')
            for i, (asset, port) in enumerate(ports, start=1):
                eqs.append(f"  connect({b.units[asset].name}.{port}, {hdr}.ports[{i}]);")
            eqs.append(f"  connect(exp{n}.port, {hdr}.ports[{len(ports) + 1}]);")
            # The header's temperature is the loop's return temperature, which every pump
            # on the loop reports as `TRet` (the World Model has no return pipework asset).
            decls.append(f"  output Real {hdr}_T;")
            eqs.append(f"  {hdr}_T = {hdr}.T;")
            outputs.extend(Variable(f"{hdr}_T", p, "TRet", "real", "K") for p in h.pumps)
            state[hdr] = {"T_start": f"{hdr}.vol.T"}
            start[hdr] = {"T_start": f"{hdr}.T_start"}
        else:
            inlet = next(i for i, _ in b.units[pump].behaviour.passages)
            decls.append(f'  GwsLib.Expansion exp{n} "pressure reference {why}";')
            eqs.append(f"  connect(exp{n}.port, {b.units[pump].name}.{inlet});")

    for name in sorted(environment):
        decls.insert(0, f'  input Real env_{name}(start=298.15) "Operating condition";')
        inputs.append(Variable(f"env_{name}", None, name, "real", "K"))

    body = "\n".join(["equation", *eqs])
    text = "\n".join([*decls, body])
    digest = hashlib.sha256(
        "\n".join([TOOLCHAIN, GWSLIB.read_text(encoding="utf-8"), text]).encode()
    ).hexdigest()
    name = f"P_{digest[:16]}"
    source = "\n".join(
        [
            f'model {name} "Generated from the World Model; do not edit"',
            *decls,
            body,
            f"end {name};",
            "",
        ]
    )
    return Partition(
        name=name,
        content_hash=digest,
        source=source,
        assets=tuple(members),
        rooms=tuple(rooms),
        inputs=tuple(inputs),
        outputs=tuple(outputs),
        state={k: v for k, v in state.items() if v},
        start={k: v for k, v in start.items() if v},
        power=power,
    )


# --- building ------------------------------------------------------------------------------

IMAGE = os.environ.get("GWS_OMC_IMAGE", "openmodelica/openmodelica:v1.25.0-minimal")
LIBDIR = os.environ.get("GWS_OMLIB", "/opt/omlib")
CACHE = Path(os.environ.get("GWS_FMU_CACHE", Path.home() / ".cache" / "gws-world-sim" / "fmu"))

SCRIPT = """setModelicaPath("{libdir}");
loadModel(Modelica, {{"4.0.0"}}); getErrorString();
loadModel(Buildings, {{"11.1.0"}}); getErrorString();
loadFile("{gwslib}"); getErrorString();
loadFile("{model}.mo"); getErrorString();
setCommandLineOptions("--fmiFlags=s:cvode"); getErrorString();
buildModelFMU({model}, version="2.0", fmuType="me", fileNamePrefix="{model}",
  platforms={{"static"}}); getErrorString();
"""


def cached(partition: Partition, cache: Path = CACHE) -> Path | None:
    fmu = cache / f"{partition.name}.fmu"
    return fmu if fmu.exists() else None


def compile_partition(partition: Partition, cache: Path = CACHE) -> tuple[Path, float]:
    """The partition's FMU, compiled unless the cache already holds it. Returns the path and
    the seconds spent compiling (0 for a cache hit)."""
    if (hit := cached(partition, cache)) is not None:
        return hit, 0.0
    cache.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{partition.name}-", dir=cache))
    (work / f"{partition.name}.mo").write_text(partition.source, encoding="utf-8")
    shutil.copy(GWSLIB, work / "GwsLib.mo")
    (work / "build.mos").write_text(
        SCRIPT.format(libdir=LIBDIR, gwslib=work / "GwsLib.mo", model=partition.name),
        encoding="utf-8",
    )
    mounts = {str(work)}
    if not LIBDIR.startswith("/opt/"):
        mounts.add(LIBDIR)
        mounts.update(str(p.resolve()) for p in Path(LIBDIR).iterdir() if p.is_symlink())
    cmd = ["docker", "run", "--rm", "-w", str(work)]
    for m in sorted(mounts):
        cmd += ["-v", f"{m}:{m}"]
    cmd += [IMAGE, "omc", "build.mos"]
    t0 = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    seconds = time.monotonic() - t0
    log = work / "build.log"
    log.write_text(result.stdout + result.stderr, encoding="utf-8")
    built = work / f"{partition.name}.fmu"
    if result.returncode != 0 or not built.exists():
        raise CompileError(f"OpenModelica failed for {partition.name}; see {log}")
    target = cache / f"{partition.name}.fmu"
    built.replace(target)
    (cache / f"{partition.name}.json").write_text(
        json.dumps(partition.manifest(), indent=1), encoding="utf-8"
    )
    shutil.rmtree(work, ignore_errors=True)
    return target, seconds


def compile_all(partitions: Iterable[Partition], cache: Path = CACHE) -> dict[str, Path]:
    return {p.name: compile_partition(p, cache)[0] for p in partitions}

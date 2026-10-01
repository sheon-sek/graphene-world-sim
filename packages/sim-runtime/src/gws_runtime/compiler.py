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
import threading
import time
import urllib.request
import zipfile
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from gws_runtime.behaviours import BEHAVIOURS, HALL, Behaviour, Input, Params, modelica_real, si
from gws_world_model.model import ROOM_PREFIX, Asset, Domain, WorldModel

GWSLIB = Path(__file__).with_name("modelica") / "GwsLib" / "package.mo"
TOOLCHAIN = "openmodelica-1.25.0+msl-4.0.0+buildings-11.1.0"
"""Part of every partition hash: a different compiler or library must rebuild."""
FLUID = frozenset({Domain.CHW, Domain.CW, Domain.AIR})
"""Domains the thermofluid models carry. Cold water service (`water`) is its own domain."""
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


def return_key(pump: str) -> str:
    """How a loop's return header appears in state: by the loop's first pump, `return:<id>`."""
    return f"return:{pump}"


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
    """Asset (`room:<id>`, `return:<pump>`) -> start parameter -> FMU variable holding it."""
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
        if _external(b, src.node, src.port) or _external(b, dst.node, dst.port):
            continue
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


def _external(b: _Build, node: str, port: str) -> bool:
    unit = b.units.get(node)
    return unit is not None and port in unit.behaviour.external


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
    for asset in sorted(b.units):
        if not any(a == asset for a, _ in linked):
            del b.units[asset]
            not_modelled[asset] = "not connected to any modelled loop"
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
    environment: dict[str, Input] = {}
    member_set = set(members)

    def io(owner: str, inst: str, behaviour: Behaviour, values: dict[str, float | bool]) -> None:
        for sig, spec in behaviour.inputs.items():
            if spec.environment:
                environment.setdefault(spec.environment, spec)
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
            key = return_key(pump)
            state[key] = {"T_start": f"{hdr}.vol.T"}
            start[key] = {"T_start": f"{hdr}.T_start"}
        else:
            inlet = next(i for i, _ in b.units[pump].behaviour.passages)
            decls.append(f'  GwsLib.Expansion exp{n} "pressure reference {why}";')
            eqs.append(f"  connect(exp{n}.port, {b.units[pump].name}.{inlet});")

    for name, spec in sorted(environment.items()):
        value = _literal(float(spec.default))
        decls.insert(0, f'  input Real env_{name}(start={value}) "Operating condition";')
        inputs.append(Variable(f"env_{name}", None, name, "real", spec.unit))

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

STOCK_IMAGE = "openmodelica/openmodelica:v1.25.0-minimal"
"""OpenModelica's own image. It ships no Modelica libraries, so it needs `GWS_OMLIB`."""
LIBRARY_IMAGE = "gws-omc:1.25"
"""The stock image with the libraries at /opt/omlib, built from spikes/phase1/Dockerfile."""
LIBDIR = os.environ.get("GWS_OMLIB", "/opt/omlib")
IMAGE = os.environ.get("GWS_OMC_IMAGE", STOCK_IMAGE if "GWS_OMLIB" in os.environ else LIBRARY_IMAGE)
LIBRARIES = ("Modelica 4.0.0", "ModelicaServices 4.0.0", "Complex 4.0.0.mo", "Buildings 11.1.0")
"""What the build script loads, laid out as `<Name> <version>` so loadModel finds each."""
SETUP = (
    "Build the image with the libraries (docker build -t gws-omc:1.25 spikes/phase1), or set "
    "GWS_OMLIB to a host directory holding " + ", ".join(LIBRARIES) + "; see README.md."
)
CACHE = Path(os.environ.get("GWS_FMU_CACHE", Path.home() / ".cache" / "gws-world-sim" / "fmu"))

SCRIPT = """setModelicaPath("{libdir}");
loadModel(Modelica, {{"4.0.0"}}); getErrorString();
loadModel(Buildings, {{"11.1.0"}}); getErrorString();
loadFile("{gwslib}"); getErrorString();
loadFile("{model}.mo"); getErrorString();
setCommandLineOptions("--fmiFlags=s:cvode"); getErrorString();
setCompiler("gws-cc"); getErrorString();
buildModelFMU({model}, version="2.0", fmuType="me", fileNamePrefix="{model}",
  platforms={{"static"}}); getErrorString();
"""

CC = r"""#!/bin/sh
# The C compiler OpenModelica builds the FMU with: clang, except for the variable table.
#
# OpenModelica runs `cmake --build . --parallel`, which is `make -j` with no limit: every C
# file of the model compiles at once, beside omc still holding the translated model. For the
# whole site that is hundreds of clang processes and more memory than most machines have, so
# a build that should take minutes swaps for hours or is killed. Each call here first takes
# one of GWS_CC_JOBS slots (a flock on a file) and holds it until clang exits.
# Waiting callers queue on one lock, so only the head of the queue polls the slots.
slots="${0%/*}/.cc-slots"
mkdir -p "$slots"
exec 8>"$slots/queue"
flock 8
held=""
while [ -z "$held" ]; do
  i=1
  while [ "$i" -le "${GWS_CC_JOBS:-2}" ]; do
    exec 9>"$slots/$i"
    if flock -n 9; then held=$i; break; fi
    exec 9>&-
    i=$((i + 1))
  done
  [ -n "$held" ] || sleep 0.2
done
exec 8>&-
# OpenModelica writes every variable's name, comment and attributes as one straight-line
# function in <model>_init_fmu.c, a million lines for a whole site, and clang's time grows
# faster than the function. This splits it into functions of 5,000 lines and compiles it
# without optimisation (it runs once, at instantiation).
for a in "$@"; do
  case "$a" in *_init_fmu.c) init="$a" ;; esac
done
if [ -n "$init" ] && ! grep -q '_read_input_fmu_0(' "$init"; then
  awk -v N=5000 '
    /^void [A-Za-z0-9_]+_read_input_fmu\(MODEL_DATA\* modelData\)$/ {
      name = $2; sub(/\(.*/, "", name); getline; k = 0; n = 0; body = 1
      print "static void " name "_0(MODEL_DATA* modelData)"; print "{"; next
    }
    body && /^}$/ {
      print "}"; print "void " name "(MODEL_DATA* modelData)"; print "{"
      for (i = 0; i <= k; i++) print "  " name "_" i "(modelData);"
      print "}"; body = 0; next
    }
    body {
      print
      if (++n >= N) {
        k++; n = 0
        print "}"; print "static void " name "_" k "(MODEL_DATA* modelData)"; print "{"
      }
      next
    }
    { print }
  ' "$init" > "$init.split" && mv "$init.split" "$init"
fi
if [ -n "$init" ]; then
  exec clang "$@" -O0
fi
exec clang "$@"
"""


def toolchain_problem(image: str = IMAGE, libdir: str = LIBDIR) -> str | None:
    """Why the OpenModelica toolchain cannot build, or None. Checked before every build, so a
    machine without the Modelica libraries fails with the fix instead of an omc scope error."""
    if not libdir.startswith("/opt/"):
        missing = [lib for lib in LIBRARIES if not (Path(libdir) / lib).exists()]
        if missing:
            return f"GWS_OMLIB={libdir} lacks {', '.join(missing)}. {SETUP}"
        return None
    if image == STOCK_IMAGE:
        return f"{image} has no Modelica libraries. {SETUP}"
    if shutil.which("docker") is None:
        return "OpenModelica runs in Docker, and docker is not on the PATH."
    found = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False)
    if found.returncode != 0:
        return f"Docker image {image} is not built. {SETUP}"
    return None


def cached(partition: Partition, cache: Path = CACHE) -> Path | None:
    fmu = cache / f"{partition.name}.fmu"
    return fmu if fmu.exists() else None


PREBUILT = os.environ.get(
    "GWS_FMU_PREBUILT",
    "https://github.com/sheon-sek/graphene-world-sim/releases/download/fmu-cache",
)
"""Where prebuilt FMUs are fetched from, by partition name, before compiling one (an empty
value turns it off). CI compiles the presets' partitions and publishes them there, so a
machine starting the whole site downloads its models in seconds instead of building them."""
COMPILE_TIMEOUT_S = float(os.environ.get("GWS_COMPILE_TIMEOUT", 3600))
"""A build that runs longer is stopped and reported, never left hanging."""
SITE_PEAK_GB = 7.4
SITE_SOURCE_CHARS = 171_052
"""omc translating the whole site's partition (121 assets) holds 7.4 GB until the build ends;
its memory grows with the size of the generated model."""
CC_JOB_GB = 1.5
"""What one clang compiling a large generated C file can take."""


def translate_gb(partition: Partition) -> float:
    return 0.5 + (SITE_PEAK_GB - 0.5) * len(partition.source) / SITE_SOURCE_CHARS


def memory_needed_gb(partition: Partition) -> float:
    """Roughly how much memory building the partition needs: omc, and one C compile."""
    return round(translate_gb(partition) + CC_JOB_GB, 1)


def cc_jobs(partition: Partition, memory_gb: float | None, cpus: int | None = None) -> int:
    """How many C files to compile at once: as many as the memory left beside omc allows,
    at most one per CPU."""
    cpus = cpus or os.cpu_count() or 2
    if memory_gb is None:
        return max(1, min(cpus, 2))
    return max(1, min(cpus, int((memory_gb - translate_gb(partition)) / CC_JOB_GB)))


@dataclass
class Build:
    """A partition being fetched or compiled, as `GET /api/runtime/builds` shows it."""

    partition: str
    assets: int
    memory_needed_gb: float
    phase: str = "waiting"
    """`downloading`, `translating` (omc loads the libraries and flattens the model; most of
    the memory), `generating` (writes C), `compiling` (C files `done` of `total`),
    `packaging`."""
    started: float = field(default_factory=time.time)
    done: int = 0
    total: int = 0
    memory_gb: float | None = None
    memory_limit_gb: float | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "partition": self.partition,
            "assets": self.assets,
            "phase": self.phase,
            "elapsed_s": round(time.time() - self.started, 1),
            "done": self.done,
            "total": self.total,
            "memory_gb": self.memory_gb,
            "memory_limit_gb": self.memory_limit_gb,
            "memory_needed_gb": self.memory_needed_gb,
        }


_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
BUILDS: dict[str, Build] = {}
"""Builds in progress, by partition name."""


def builds() -> list[dict[str, object]]:
    with _GUARD:
        return [b.to_json() for b in BUILDS.values()]


def _lock(name: str) -> threading.Lock:
    with _GUARD:
        return _LOCKS.setdefault(name, threading.Lock())


def fetch_prebuilt(partition: Partition, cache: Path = CACHE, base: str = PREBUILT) -> Path | None:
    """The partition's FMU from the prebuilt store, or None when it has none (or is offline)."""
    if not base:
        return None
    cache.mkdir(parents=True, exist_ok=True)
    tmp = cache / f"{partition.name}.fmu.part"
    try:
        with urllib.request.urlopen(f"{base}/{partition.name}.fmu", timeout=20) as r:
            with tmp.open("wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
        with zipfile.ZipFile(tmp) as z:
            if "modelDescription.xml" not in z.namelist():
                raise zipfile.BadZipFile("no modelDescription.xml")
    except (OSError, zipfile.BadZipFile):  # 404, offline, a broken download: build it here
        tmp.unlink(missing_ok=True)
        return None
    target = cache / f"{partition.name}.fmu"
    tmp.replace(target)
    (cache / f"{partition.name}.json").write_text(
        json.dumps(partition.manifest(), indent=1), encoding="utf-8"
    )
    return target


def docker_memory_gb() -> float | None:
    """The memory Docker can give a container, or None when Docker does not say."""
    try:
        out = subprocess.run(
            ["docker", "info", "--format", "{{.MemTotal}}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        ).stdout.strip()
        return round(int(out) / 2**30, 1) if out.isdigit() and int(out) > 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


_SIZE = re.compile(r"([\d.]+)\s*([KMGT]?i?B)")
_UNITS = {
    "B": 1,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
    "TiB": 2**40,
    "KB": 1e3,
    "MB": 1e6,
    "GB": 1e9,
    "TB": 1e12,
}


def _container_memory_gb(container: str) -> float | None:
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _SIZE.search(out)
    return round(float(m.group(1)) * _UNITS.get(m.group(2), 1) / 2**30, 2) if m else None


def _progress(work: Path, build: Build) -> None:
    """Read how far omc is from the files it has written."""
    sources = next(work.glob("*.fmutmp/sources"), None)
    if sources is None:
        build.phase = "translating"
        return
    if next(sources.parent.glob("binaries/*"), None) is not None:
        build.phase = "packaging"
        return
    c = sum(1 for _ in sources.rglob("*.c"))
    cmake = next(sources.glob("build_cmake*"), None)
    if cmake is None:
        build.phase, build.done, build.total = "generating", c, 0
        return
    build.phase, build.total = "compiling", c
    build.done = min(c, sum(1 for _ in cmake.rglob("*.o")))


def compile_partition(partition: Partition, cache: Path = CACHE) -> tuple[Path, float]:
    """The partition's FMU: from the cache, else the prebuilt store, else compiled here.
    Returns the path and the seconds spent compiling (0 when it was not compiled). One build
    runs per partition at a time; a second caller waits for it and gets its result."""
    if (hit := cached(partition, cache)) is not None:
        return hit, 0.0
    with _lock(partition.name):
        if (hit := cached(partition, cache)) is not None:
            return hit, 0.0
        build = Build(partition.name, len(partition.assets), memory_needed_gb(partition))
        with _GUARD:
            BUILDS[partition.name] = build
        try:
            build.phase = "downloading"
            if (fetched := fetch_prebuilt(partition, cache)) is not None:
                return fetched, 0.0
            return _compile(partition, cache, build)
        finally:
            with _GUARD:
                BUILDS.pop(partition.name, None)


def _compile(partition: Partition, cache: Path, build: Build) -> tuple[Path, float]:
    if (problem := toolchain_problem()) is not None:
        raise CompileError(f"OpenModelica cannot build {partition.name}: {problem}")
    build.memory_limit_gb = docker_memory_gb()
    if build.memory_limit_gb is not None and build.memory_limit_gb < build.memory_needed_gb:
        raise CompileError(
            f"Building {partition.name} ({len(partition.assets)} assets) needs about "
            f"{build.memory_needed_gb} GB of memory, and Docker can use "
            f"{build.memory_limit_gb} GB. Give Docker more memory (Docker Desktop: Settings > "
            "Resources; WSL: memory= in .wslconfig), or start a smaller scope."
        )
    build.phase = "translating"
    cache.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{partition.name}-", dir=cache))
    (work / f"{partition.name}.mo").write_text(partition.source, encoding="utf-8")
    shutil.copy(GWSLIB, work / "GwsLib.mo")
    cc = work / "gws-cc"
    cc.write_text(CC, encoding="utf-8", newline="\n")
    cc.chmod(0o755)
    (work / "build.mos").write_text(
        SCRIPT.format(libdir=LIBDIR, gwslib=work / "GwsLib.mo", model=partition.name),
        encoding="utf-8",
    )
    mounts = {str(work)}
    if not LIBDIR.startswith("/opt/"):
        mounts.add(LIBDIR)
        mounts.update(str(p.resolve()) for p in Path(LIBDIR).iterdir() if p.is_symlink())
    # OpenModelica passes the compiler to CMake by name, so the wrapper goes on the PATH.
    path = f"{work}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    container = f"gws-omc-{work.name}"
    jobs = cc_jobs(partition, build.memory_limit_gb)
    cmd = ["docker", "run", "--rm", "--name", container, "-w", str(work), "-e", f"PATH={path}"]
    cmd += ["-e", f"GWS_CC_JOBS={jobs}"]
    for m in sorted(mounts):
        cmd += ["-v", f"{m}:{m}"]
    cmd += [IMAGE, "omc", "build.mos"]
    log = work / "build.log"
    t0 = time.monotonic()
    with log.open("w", encoding="utf-8") as out:
        proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, text=True)
        polls = 0
        while proc.poll() is None:
            if time.monotonic() - t0 > COMPILE_TIMEOUT_S:
                subprocess.run(["docker", "kill", container], capture_output=True, check=False)
                proc.kill()
                proc.wait()
                raise CompileError(
                    f"Building {partition.name} took longer than {COMPILE_TIMEOUT_S / 60:.0f} "
                    f"minutes and was stopped (GWS_COMPILE_TIMEOUT); see {log}"
                )
            _progress(work, build)
            if polls % 3 == 0:
                build.memory_gb = _container_memory_gb(container) or build.memory_gb
            polls += 1
            time.sleep(2.0)
    seconds = time.monotonic() - t0
    text = log.read_text(encoding="utf-8", errors="replace")
    built = work / f"{partition.name}.fmu"
    if proc.returncode == 137:
        raise CompileError(
            f"OpenModelica was killed building {partition.name} (exit 137), almost always "
            f"because memory ran out (it was using {build.memory_gb or '?'} GB). It needs "
            f"about {build.memory_needed_gb} GB; give Docker more memory, close other large "
            "programs, or start a smaller scope."
        )
    if proc.returncode != 0 or not built.exists():
        tail = "\n".join(text.strip().splitlines()[-20:])
        raise CompileError(f"OpenModelica failed for {partition.name}; see {log}\n{tail}")
    target = cache / f"{partition.name}.fmu"
    built.replace(target)
    (cache / f"{partition.name}.json").write_text(
        json.dumps(partition.manifest(), indent=1), encoding="utf-8"
    )
    shutil.rmtree(work, ignore_errors=True)
    return target, seconds


def compile_all(partitions: Iterable[Partition], cache: Path = CACHE) -> dict[str, Path]:
    return {p.name: compile_partition(p, cache)[0] for p in partitions}

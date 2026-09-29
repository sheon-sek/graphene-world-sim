"""Generate a Modelica model from a World Model fragment.

The fragment lists assets (id, ComponentType, parameters, initial input values) and
port-level connections. Each ComponentType maps to one hand-written equipment model in
GwsLib. Every equipment input becomes a top-level FMU input and every equipment output a
top-level FMU output, named ``<asset>_<signal>``. The runtime drives those inputs (commands,
faults, electrical availability) and reads those outputs (points).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ComponentType:
    modelica_class: str
    real_inputs: tuple[str, ...] = ()
    bool_inputs: tuple[str, ...] = ()
    real_outputs: tuple[str, ...] = ()
    bool_outputs: tuple[str, ...] = ()
    water_ports: tuple[str, ...] = ()
    pump_inlet: str | None = None
    environment_inputs: dict[str, str] = field(default_factory=dict)
    power_signal: str | None = None
    """Output (or input) that carries the asset's electrical demand in W. Powered types also
    get a ``V_pu`` input: the supply voltage the electrical solver computed for their bus."""
    motor: bool = True
    state: dict[str, str] = field(default_factory=dict)
    """Start parameter -> internal variable. A snapshot reads the variable from a running FMU
    and a rebuilt FMU receives it as the start parameter, keyed by World Model asset id."""


TYPES: dict[str, ComponentType] = {
    "Chiller": ComponentType(
        "GwsLib.Chiller",
        real_inputs=("TChwSet",),
        bool_inputs=("enable",),
        real_outputs=("P", "QEva", "TChwLvg", "TCwLvg"),
        bool_outputs=("running",),
        water_ports=("chw_in", "chw_out", "cw_in", "cw_out"),
        power_signal="P",
        state={"TChw_start": "chi.vol2.T", "TCw_start": "chi.vol1.T"},
    ),
    "Chiller Pump": ComponentType(
        "GwsLib.Pump",
        real_inputs=("speed",),
        real_outputs=("P", "m_flow"),
        water_ports=("inlet", "outlet"),
        pump_inlet="inlet",
        power_signal="P",
        state={"T_start": "mov.heatPort.T"},
    ),
    "Chiller Valve": ComponentType(
        "GwsLib.Valve", real_inputs=("position",), water_ports=("inlet", "outlet")
    ),
    "Buffer Tank": ComponentType(
        "GwsLib.BufferTank",
        real_outputs=("T",),
        water_ports=("inlet", "outlet"),
        state={"T_start": "vol.T"},
    ),
    "Cooling Tower": ComponentType(
        "GwsLib.CoolingTower",
        real_inputs=("fanSpeed",),
        real_outputs=("PFan", "TLvg"),
        water_ports=("inlet", "outlet"),
        environment_inputs={"TWetBulb": "TWetBulb"},
        power_signal="PFan",
        state={"T_start": "tow.vol.T"},
    ),
    "FCU": ComponentType(
        "GwsLib.FanCoil",
        real_inputs=("fanSpeed",),
        real_outputs=("Q", "PFan", "TSupAir"),
        water_ports=("chw_in", "chw_out"),
        power_signal="PFan",
        state={"TRet_start": "fan.mov.heatPort.T", "TSup_start": "TSup.T"},
    ),
    "Data Hall": ComponentType(
        "GwsLib.DataHall",
        real_inputs=("QIt",),
        real_outputs=("TAir", "TMass"),
        power_signal="QIt",
        motor=False,
        state={"T_start": "vol.T", "TMass_start": "mass.T"},
    ),
}


def ident(asset_id: str) -> str:
    """Modelica identifier for an asset path, e.g. 'Chiller/R_C1' -> 'Chiller_R_C1'."""
    name = re.sub(r"[^A-Za-z0-9_]", "_", asset_id)
    return name if name[0].isalpha() else f"a_{name}"


def _literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return value  # record constructor or expression, emitted verbatim
    raise TypeError(f"unsupported parameter value {value!r}")


def _port(ref: str, assets: dict[str, dict[str, object]]) -> tuple[str, str]:
    asset_id, _, port = ref.rpartition(".")
    if asset_id not in assets:
        raise ValueError(f"connection references unknown asset {asset_id!r}")
    return asset_id, port


def _water_loops(fragment: dict[str, object]) -> list[set[str]]:
    """Group water-side assets into closed loops by following water connections."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    for c in fragment["connections"]:  # type: ignore[attr-defined]
        if c["medium"] in ("chw", "cw"):
            # A chiller sits in two loops, so loops are keyed by asset *and* medium.
            a = f"{c['from'].rpartition('.')[0]}#{c['medium']}"
            b = f"{c['to'].rpartition('.')[0]}#{c['medium']}"
            parent[find(a)] = find(b)
    loops: dict[str, set[str]] = {}
    for node in list(parent):
        loops.setdefault(find(node), set()).add(node)
    return list(loops.values())


def generate(fragment: dict[str, object]) -> tuple[str, dict[str, object]]:
    """Return (Modelica source, point map) for a World Model fragment."""
    model = str(fragment["name"])
    assets = {str(a["id"]): a for a in fragment["assets"]}  # type: ignore[attr-defined]
    environment: dict[str, float] = dict(fragment.get("environment", {}))  # type: ignore[call-overload]

    decls: list[str] = []
    eqs: list[str] = []
    inputs: dict[str, dict[str, object]] = {}
    outputs: dict[str, dict[str, str]] = {}

    for name, value in environment.items():
        decls.append(f'  input Real env_{name}(start={value!r}) "Environment";')
        inputs[f"env_{name}"] = {"asset": None, "signal": name, "start": value}

    for asset_id, asset in assets.items():
        ctype = TYPES[str(asset["type"])]
        inst = ident(asset_id)
        params = ", ".join(f"{k}={_literal(v)}" for k, v in dict(asset.get("params", {})).items())
        decls.append(f'  {ctype.modelica_class} {inst}({params}) "{asset_id}";')
        given = dict(asset.get("inputs", {}))
        if ctype.power_signal and ctype.motor:
            given.setdefault("V_pu", 1.0)
        powered = ("V_pu",) if "V_pu" in given else ()
        for sig in ctype.real_inputs + powered + ctype.bool_inputs:
            if sig not in given:
                raise ValueError(f"{asset_id}: missing initial value for input {sig!r}")
            kind = "Boolean" if sig in ctype.bool_inputs else "Real"
            var = f"{inst}_{sig}"
            decls.append(f"  input {kind} {var}(start={_literal(given[sig])});")
            eqs.append(f"  {inst}.{sig} = {var};")
            inputs[var] = {"asset": asset_id, "signal": sig, "start": given[sig]}
        for sig, env in ctype.environment_inputs.items():
            eqs.append(f"  {inst}.{sig} = env_{env};")
        for sig in ctype.real_outputs + ctype.bool_outputs:
            kind = "Boolean" if sig in ctype.bool_outputs else "Real"
            var = f"{inst}_{sig}"
            decls.append(f"  output {kind} {var};")
            eqs.append(f"  {var} = {inst}.{sig};")
            outputs[var] = {"asset": asset_id, "signal": sig}

    for c in fragment["connections"]:  # type: ignore[attr-defined]
        a, pa = _port(c["from"], assets)
        b, pb = _port(c["to"], assets)
        eqs.append(f"  connect({ident(a)}.{pa}, {ident(b)}.{pb});")

    # Each closed water loop needs exactly one pressure reference: an expansion vessel at
    # the inlet of the loop's first pump (by asset id, so generation is deterministic).
    for n, loop in enumerate(sorted(_water_loops(fragment), key=sorted)):
        medium = next(iter(loop)).split("#")[1]
        pumps = sorted(
            node.split("#")[0]
            for node in loop
            if TYPES[str(assets[node.split("#")[0]]["type"])].pump_inlet
        )
        if not pumps:
            raise ValueError(f"{medium} loop {sorted(loop)} has no pump")
        pump = pumps[0]
        port = TYPES[str(assets[pump]["type"])].pump_inlet
        decls.append(f'  GwsLib.Expansion exp{n} "{medium} loop pressure reference";')
        eqs.append(f"  connect(exp{n}.port, {ident(pump)}.{port});")

    source = "\n".join(
        [
            f'model {model} "Generated from World Model fragment; do not edit"',
            *decls,
            "equation",
            *eqs,
            f"end {model};",
            "",
        ]
    )
    power = {
        asset_id: f"{ident(asset_id)}_{TYPES[str(a['type'])].power_signal}"
        for asset_id, a in assets.items()
        if TYPES[str(a["type"])].power_signal
    }
    state = {
        asset_id: {p: f"{ident(asset_id)}.{v}" for p, v in TYPES[str(a["type"])].state.items()}
        for asset_id, a in assets.items()
    }
    return source, {
        "model": model,
        "inputs": inputs,
        "outputs": outputs,
        "power": power,
        "state": state,
    }


def main() -> None:
    fragment_path, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
    fragment = json.loads(fragment_path.read_text())
    source, point_map = generate(fragment)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{point_map['model']}.mo").write_text(source)
    (out_dir / f"{point_map['model']}.points.json").write_text(json.dumps(point_map, indent=2))
    print(out_dir / f"{point_map['model']}.mo")


if __name__ == "__main__":
    main()

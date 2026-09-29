"""The electrical network: the World Model's power connections as one pandapower AC load flow.

Mapping (by ComponentType behaviour, else by category and power ports; never by asset id):

- `UtilitySupply`: an HV bus with an ext_grid (in service while utility power is available)
  and a transformer to its LV bus.
- `Genset`: a bus with an ext_grid, in service while the set runs. Parallel sets on one bus
  share load equally.
- `UPS`: an input bus with a load (the rectifier's draw) and an output bus with an ext_grid
  (the inverter, in service while it runs from its input or its battery).
- `TransferSwitch`: normal, emergency and load buses joined by two interlocked bus-bus
  switches.
- `Feeder` (meters, branch monitors): one bus; its breaker opens the load end of every
  connection into it.
- Equipment with only a power input, and rooms: one bus with a load whose demand the master
  sets; with several feeds, one bus and load per feed (a power cord).
- Anything else with power ports: one bus.

Every power connection is a short cable (a line; `r_ohm`/`x_ohm` connection parameters, else
typed defaults). Everything is on one 0.4 kV level below the utility transformers.

A load with several feeds (an IT load on three UPS branch circuits) shares its demand equally
among its energised cords, as dual-corded power supplies do, and never backfeeds a dead feed.
Its supply is its best cord's voltage.

The UPS inverter being a source decouples its output from its input: the input load is
(output + recharge) / efficiency, so each step re-solves until the UPS modes and input demands
are stable (normally one extra pass). A solve is skipped when no source or switch changed and
no load moved beyond the deadband since the last one, as in the Phase 1 spike.
"""

from __future__ import annotations

import math
import warnings
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandapower as pp

from gws_runtime.electrical.equipment import (
    BEHAVIOUR_FEEDER,
    BEHAVIOUR_GENSET,
    BEHAVIOUR_TRANSFER_SWITCH,
    BEHAVIOUR_UPS,
    BEHAVIOUR_UTILITY,
    POSITION_EMERGENCY,
    POSITION_NORMAL,
    Faults,
    Feeder,
    Genset,
    TransferSwitch,
    Ups,
    UtilitySupply,
)
from gws_world_model.model import (
    ROOM_PREFIX,
    AssetCategory,
    ComponentType,
    Direction,
    Domain,
    Scalar,
    WorldModel,
)

type Floats = npt.NDArray[np.float64]

V_LV_KV = 0.4
ENERGISED_PU = 0.5
"""A bus above this voltage counts as energised."""
LOAD_DEADBAND = 0.01
"""Relative load change that forces a re-solve."""
LOAD_FLOOR_W = 100.0
"""Absolute load change that always forces a re-solve."""
CABLE_R_OHM = 0.0002
CABLE_X_OHM = 0.0001
"""Default cable impedance of a connection (assumed: short, heavy LV cable or busbar)."""
DEFAULT_POWER_FACTOR = 0.95
MAX_PASSES = 6
SQRT3 = math.sqrt(3.0)

UTILITY, GENSET, UPS, ATS, FEEDER, LOAD, NODE = (
    "utility",
    "genset",
    "ups",
    "ats",
    "feeder",
    "load",
    "node",
)
_BEHAVIOURS = {
    BEHAVIOUR_UTILITY: UTILITY,
    BEHAVIOUR_GENSET: GENSET,
    BEHAVIOUR_UPS: UPS,
    BEHAVIOUR_TRANSFER_SWITCH: ATS,
    BEHAVIOUR_FEEDER: FEEDER,
}


def _power_ports(ctype: ComponentType) -> dict[str, Direction]:
    return {n: p.direction for n, p in ctype.ports.items() if p.domain is Domain.POWER}


def _kind(ctype: ComponentType) -> str:
    if ctype.behaviour in _BEHAVIOURS:
        return _BEHAVIOURS[ctype.behaviour]
    ports = _power_ports(ctype).values()
    has_in = any(d is not Direction.OUT for d in ports)
    has_out = any(d is not Direction.IN for d in ports)
    if has_in and not has_out and ctype.category is AssetCategory.EQUIPMENT:
        return LOAD
    return NODE


def _nan0(values: Any) -> Floats:
    array: Floats = np.nan_to_num(np.asarray(values, dtype=np.float64), nan=0.0)
    return array


@dataclass(slots=True)
class _Solved:
    ext: npt.NDArray[np.bool_]
    switch: npt.NDArray[np.bool_]
    load: Floats


class ElectricalNetwork:
    """The electrical domain of one World Model revision (or a scope of it)."""

    def __init__(self, doc: WorldModel, nodes: Collection[str]) -> None:
        self._doc = doc
        self._kind: dict[str, str] = {}
        self._params: dict[str, dict[str, Scalar]] = {}
        for node in sorted(nodes):
            if node.startswith(ROOM_PREFIX):
                self._kind[node] = LOAD
                self._params[node] = {}
                continue
            asset = doc.assets[node]
            ctype = doc.component_types[asset.type]
            self._kind[node] = _kind(ctype)
            self._params[node] = {k: s.default for k, s in ctype.parameters.items()} | dict(
                asset.parameters
            )
        self._feeds: dict[str, int] = defaultdict(int)
        for c in doc.connections.values():
            if c.domain is Domain.POWER and c.source.node in self._kind:
                self._feeds[c.target.node] += 1
        self.assets: frozenset[str] = frozenset(n for n in self._kind if n in doc.assets)
        self.loads: frozenset[str] = frozenset(n for n, k in self._kind.items() if k == LOAD)

        self._utility = True
        self._faults: dict[str, dict[str, dict[str, float]]] = {}
        self._demand: dict[str, float] = dict.fromkeys(sorted(self.loads), 0.0)
        self._time = 0.0

        self._feeders: dict[str, Feeder] = {}
        self._gensets: dict[str, Genset] = {}
        self._ups: dict[str, Ups] = {}
        self._ats: dict[str, TransferSwitch] = {}
        self._supplies: dict[str, UtilitySupply] = {}

        self._net = pp.create_empty_network()
        self._bus_names: list[str] = []
        self._main_bus: dict[str, int] = {}
        self._cords: dict[str, list[int]] = {}
        """Per-feed buses of a load with several feeds, in connection id order."""
        self._port_bus: dict[tuple[str, str], int] = {}
        self._ext_names: list[str] = []
        self._ext_of: dict[str, int] = {}
        self._switch_names: list[str] = []
        self._feeder_switches: dict[str, list[int]] = defaultdict(list)
        self._ats_switches: dict[str, tuple[int, int]] = {}
        self._ats_buses: dict[str, tuple[int, int, int]] = {}
        self._ups_buses: dict[str, tuple[int, int]] = {}
        self._load_names: list[str] = []
        self._load_of: dict[str, int] = {}
        self._q_ratio: list[float] = []
        self._in_lines: dict[str, list[int]] = defaultdict(list)
        self._out_lines: dict[str, list[int]] = defaultdict(list)
        for node in sorted(self._kind):
            self._build_node(node)
        self._build_connections()

        self._solved: _Solved | None = None
        self._vm: Floats = np.zeros(len(self._bus_names))
        self._line_p: Floats = np.zeros((len(self._net.line), 4))
        self._ext_pq: Floats = np.zeros((len(self._ext_names), 2))
        self._load_pq: Floats = np.zeros((len(self._load_names), 2))
        self.solves = 0
        """Number of load-flow solutions so far (for diagnostics)."""
        self._settle()

    # --- construction ------------------------------------------------------------------------

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str] | None = None) -> ElectricalNetwork:
        """Build from the World Model's power connections. With a scope, only those assets
        (and rooms, as `room:<id>`) plus the upstream path that supplies them are modelled."""
        power = [c for c in doc.connections.values() if c.domain is Domain.POWER]
        nodes = {c.source.node for c in power} | {c.target.node for c in power}
        nodes |= {a.id for a in doc.assets.values() if _power_ports(doc.component_types[a.type])}
        if scope is not None:
            feeds: dict[str, set[str]] = defaultdict(set)
            for c in power:
                feeds[c.target.node].add(c.source.node)
            keep: set[str] = set()
            todo = [n for n in scope if n in nodes]
            while todo:
                node = todo.pop()
                if node not in keep:
                    keep.add(node)
                    todo.extend(feeds[node])
            nodes = keep
        return cls(doc, nodes)

    def _param(self, node: str, name: str, default: float) -> float:
        value = self._params[node].get(name, default)
        return float(value) if isinstance(value, int | float) else default

    def _bus(self, name: str, vn_kv: float = V_LV_KV) -> int:
        self._bus_names.append(name)
        return int(pp.create_bus(self._net, vn_kv=vn_kv, name=name))

    def _ext(self, node: str, bus: int, vm_pu: float = 1.0) -> None:
        self._ext_of[node] = len(self._ext_names)
        self._ext_names.append(node)
        pp.create_ext_grid(self._net, bus, vm_pu=vm_pu, name=node, in_service=False)

    def _load(self, name: str, bus: int, power_factor: float) -> None:
        self._load_of[name] = len(self._load_names)
        self._load_names.append(name)
        pf = min(max(power_factor, 0.1), 1.0)
        self._q_ratio.append(math.tan(math.acos(pf)))
        pp.create_load(self._net, bus, p_mw=0.0, q_mvar=0.0, name=name)

    def _build_node(self, node: str) -> None:
        kind = self._kind[node]
        ports: dict[str, Direction] = {}
        if node in self._doc.assets:
            ports = _power_ports(self._doc.component_types[self._doc.assets[node].type])
        if kind == UTILITY:
            lv = self._bus(node)
            hv = self._bus(f"{node}#hv", self._param(node, "voltage_hv", 11.0))
            pp.create_transformer_from_parameters(
                self._net,
                hv,
                lv,
                sn_mva=self._param(node, "s_rated", 5000.0) / 1000,
                vn_hv_kv=self._param(node, "voltage_hv", 11.0),
                vn_lv_kv=V_LV_KV,
                vk_percent=self._param(node, "vk_percent", 6.0),
                vkr_percent=self._param(node, "vkr_percent", 1.0),
                pfe_kw=0.0,
                i0_percent=0.0,
                name=node,
            )
            self._ext(node, hv, self._param(node, "vm_pu", 1.0))
            self._supplies[node] = UtilitySupply()
            self._main_bus[node] = lv
        elif kind == GENSET:
            bus = self._bus(node)
            self._ext(node, bus)
            self._gensets[node] = Genset(
                p_prime_w=self._param(node, "p_prime", 3000.0) * 1000,
                start_time=self._param(node, "start_time", 10.0),
                overload_limit=self._param(node, "overload_limit", 1.1),
                overload_time=self._param(node, "overload_time", 10.0),
            )
            self._main_bus[node] = bus
        elif kind == UPS:
            bus_in, bus_out = self._bus(f"{node}#in"), self._bus(node)
            for port, direction in ports.items():
                self._port_bus[(node, port)] = bus_out if direction is Direction.OUT else bus_in
            efficiency = self._param(node, "efficiency", 0.96)
            p_rated = (
                self._param(node, "s_rated", 800.0) * 1000 * self._param(node, "power_factor", 1.0)
            )
            capacity = p_rated * self._param(node, "battery_autonomy", 480.0) / efficiency
            input_pf = self._param(node, "input_power_factor", 0.99)
            self._load(f"{node}#in", bus_in, input_pf)
            self._ext(node, bus_out)
            self._ups[node] = Ups(
                p_rated_w=p_rated,
                efficiency=efficiency,
                capacity_j=capacity,
                recharge_w=self._param(node, "recharge_fraction", 0.1) * p_rated,
                vin_min=self._param(node, "input_voltage_min", 0.85),
                input_power_factor=input_pf,
                energy_j=capacity * self._param(node, "state_of_charge_start", 1.0),
            )
            self._ups_buses[node] = (bus_in, bus_out)
            self._main_bus[node] = bus_out
        elif kind == ATS:
            normal, emergency = self._bus(f"{node}#normal"), self._bus(f"{node}#emergency")
            out = self._bus(node)
            self._port_bus[(node, "normal_in")] = normal
            self._port_bus[(node, "emergency_in")] = emergency
            self._port_bus[(node, "power_out")] = out
            switches = []
            for side, bus in (("normal", normal), ("emergency", emergency)):
                switches.append(len(self._switch_names))
                self._switch_names.append(f"{node}#{side}")
                pp.create_switch(self._net, bus, out, et="b", closed=False, name=f"{node}#{side}")
            self._ats_switches[node] = (switches[0], switches[1])
            self._ats_buses[node] = (normal, emergency, out)
            self._ats[node] = TransferSwitch(round(self._param(node, "initial_position", 1)))
            self._main_bus[node] = out
        else:
            bus = self._bus(node)
            self._main_bus[node] = bus
            if kind == FEEDER:
                self._feeders[node] = Feeder()
            elif kind == LOAD:
                pf = self._param(node, "power_factor", DEFAULT_POWER_FACTOR)
                feeds = self._feeds.get(node, 0)
                if feeds < 2:
                    self._load(node, bus, pf)
                else:
                    self._cords[node] = [bus] + [
                        self._bus(f"{node}#cord{i}") for i in range(1, feeds)
                    ]
                    for i, cord in enumerate(self._cords[node]):
                        self._load(f"{node}#cord{i}", cord, pf)

    def _build_connections(self) -> None:
        cords = {node: iter(buses) for node, buses in self._cords.items()}
        for cid in sorted(self._doc.connections):
            c = self._doc.connections[cid]
            src, dst = c.source.node, c.target.node
            if c.domain is not Domain.POWER or src not in self._kind or dst not in self._kind:
                continue
            from_bus = self._port_bus.get((src, c.source.port), self._main_bus[src])
            if dst in cords:
                to_bus = next(cords[dst])
            else:
                to_bus = self._port_bus.get((dst, c.target.port), self._main_bus[dst])
            r = c.parameters.get("r_ohm", CABLE_R_OHM)
            x = c.parameters.get("x_ohm", CABLE_X_OHM)
            line = int(
                pp.create_line_from_parameters(
                    self._net,
                    from_bus,
                    to_bus,
                    length_km=1.0,
                    r_ohm_per_km=float(r) if isinstance(r, int | float) else CABLE_R_OHM,
                    x_ohm_per_km=float(x) if isinstance(x, int | float) else CABLE_X_OHM,
                    c_nf_per_km=0.0,
                    max_i_ka=100.0,
                    name=cid,
                )
            )
            self._out_lines[src].append(line)
            self._in_lines[dst].append(line)
            if self._kind[dst] == FEEDER:
                self._feeder_switches[dst].append(len(self._switch_names))
                self._switch_names.append(cid)
                pp.create_switch(self._net, to_bus, line, et="l", closed=True, name=cid)

    # --- inputs ------------------------------------------------------------------------------

    def set_demand(self, asset: str, p_w: float) -> None:
        """Real power demand of a load in W, set every step by the master."""
        if asset not in self._demand:
            raise KeyError(f"{asset!r} is not a load of this network")
        self._demand[asset] = max(float(p_w), 0.0)

    def set_utility(self, available: bool) -> None:
        self._utility = bool(available)

    def _faults_of(self, asset: str) -> Faults:
        return self._faults.get(asset, {})

    def fault(self, asset: str, mode: str, params: Mapping[str, float]) -> None:
        """Activate one of the asset's own fault modes. Modes the electrical model does not act
        on (sensor errors, load trips owned by the equipment models) are accepted and kept."""
        if asset not in self.assets:
            raise KeyError(f"{asset!r} is not in this electrical network")
        spec = self._doc.component_types[self._doc.assets[asset].type].fault_modes.get(mode)
        if spec is None:
            raise ValueError(f"{self._doc.assets[asset].type} has no fault mode {mode!r}")
        values = {
            k: float(s.default)
            for k, s in spec.parameters.items()
            if isinstance(s.default, int | float)
        } | {k: float(v) for k, v in params.items()}
        self._faults.setdefault(asset, {})[mode] = values
        if asset in self._feeders:
            self._feeders[asset].advance(self._faults_of(asset))

    def clear(self, asset: str, mode: str) -> None:
        """Remove the fault's cause. Latched protection (breaker trip, genset lockout) stays
        until the asset is reset."""
        modes = self._faults.get(asset, {})
        modes.pop(mode, None)
        if not modes:
            self._faults.pop(asset, None)

    def command(self, asset: str, signal: str, value: float | bool) -> None:
        """An operator or controller command: feeder `breaker`/`reset`, genset `start`/`reset`,
        transfer switch `position`. It acts at the next step."""
        faults = self._faults_of(asset)
        equipment: Feeder | Genset | TransferSwitch | Ups | UtilitySupply | None = (
            self._feeders.get(asset)
            or self._gensets.get(asset)
            or self._ats.get(asset)
            or self._ups.get(asset)
            or self._supplies.get(asset)
        )
        if equipment is None:
            raise KeyError(f"{asset!r} takes no electrical commands")
        equipment.command(signal, value, faults)

    # --- stepping ----------------------------------------------------------------------------

    def step(self, t: float, dt: float) -> None:
        """Advance equipment dynamics over (t - dt, t] from the last solution, then solve at t."""
        for name, ups in self._ups.items():
            ups.integrate(dt, self._faults_of(name))
        for name, genset in self._gensets.items():
            genset.advance(dt, self._faults_of(name))
        for name, feeder in self._feeders.items():
            feeder.advance(self._faults_of(name))
        self._time = t
        self._settle()

    def _settle(self) -> None:
        for _ in range(MAX_PASSES):
            solved = self._solve_if_needed()
            changed = False
            for name, ups in self._ups.items():
                vin = float(self._vm[self._ups_buses[name][0]])
                changed |= ups.update_mode(vin, self._faults_of(name))
            if not solved and not changed:
                return

    def _state(self) -> _Solved:
        ext = np.zeros(len(self._ext_names), dtype=bool)
        for name, supply in self._supplies.items():
            ext[self._ext_of[name]] = supply.available(self._utility, self._faults_of(name))
        for name, genset in self._gensets.items():
            ext[self._ext_of[name]] = genset.running
        for name, ups in self._ups.items():
            ext[self._ext_of[name]] = ups.inverter_on
        switch = np.ones(len(self._switch_names), dtype=bool)
        for name, feeder in self._feeders.items():
            for idx in self._feeder_switches[name]:
                switch[idx] = feeder.closed
        for name, ats in self._ats.items():
            normal, emergency = self._ats_switches[name]
            switch[normal] = ats.position == POSITION_NORMAL
            switch[emergency] = ats.position == POSITION_EMERGENCY
        load = np.zeros(len(self._load_names))
        for name, p in self._demand.items():
            if name in self._cords:
                buses = self._cords[name]
                live = [self._vm[b] >= ENERGISED_PU for b in buses]
                if not any(live):
                    live = [True] * len(buses)
                share = p / sum(live)
                for i, on in enumerate(live):
                    load[self._load_of[f"{name}#cord{i}"]] = share if on else 0.0
            else:
                load[self._load_of[name]] = p
        for name, ups in self._ups.items():
            load[self._load_of[f"{name}#in"]] = ups.input_w(self._faults_of(name))
        return _Solved(ext, switch, load)

    def _solve_if_needed(self) -> bool:
        state = self._state()
        last = self._solved
        if (
            last is not None
            and np.array_equal(state.ext, last.ext)
            and np.array_equal(state.switch, last.switch)
            and bool(
                np.all(
                    np.abs(state.load - last.load)
                    <= np.maximum(LOAD_DEADBAND * np.abs(last.load), LOAD_FLOOR_W)
                )
            )
        ):
            return False
        self._solve(state)
        return True

    def _solve(self, state: _Solved) -> None:
        net = self._net
        if len(self._ext_names):
            net.ext_grid["in_service"] = state.ext
        if len(self._switch_names):
            net.switch["closed"] = state.switch
        if len(self._load_names):
            p_mw = state.load / 1e6
            net.load["p_mw"] = p_mw
            net.load["q_mvar"] = p_mw * np.asarray(self._q_ratio)
        self.solves += 1
        self._solved = state
        if not state.ext.any():
            self._vm = np.zeros(len(self._bus_names))
            self._line_p = np.zeros((len(net.line), 4))
            self._ext_pq = np.zeros((len(self._ext_names), 2))
            self._load_pq = np.zeros((len(self._load_names), 2))
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pp.runpp(net, numba=False)
            self._vm = _nan0(net.res_bus.vm_pu.to_numpy())
            self._line_p = (
                _nan0(
                    net.res_line[["p_from_mw", "q_from_mvar", "p_to_mw", "q_to_mvar"]].to_numpy()
                ).reshape(-1, 4)
                * 1e6
            )
            self._ext_pq = (
                _nan0(net.res_ext_grid[["p_mw", "q_mvar"]].to_numpy()).reshape(-1, 2) * 1e6
            )
            self._load_pq = _nan0(net.res_load[["p_mw", "q_mvar"]].to_numpy()).reshape(-1, 2) * 1e6
            self._ext_pq[~state.ext] = 0.0
        for name, ups in self._ups.items():
            ups.p_out_w = float(self._ext_pq[self._ext_of[name], 0])
        for name, genset in self._gensets.items():
            genset.p_w = float(self._ext_pq[self._ext_of[name], 0])

    # --- outputs -----------------------------------------------------------------------------

    def supply(self, asset: str) -> float:
        """Per-unit voltage at the asset's supply terminals, 0.0 when de-energised; for a load
        with several feeds, its best energised feed."""
        buses = self._cords.get(asset, [self._main_bus[asset]])
        return float(self._vm[buses].max())

    def _flow(self, node: str) -> tuple[float, float]:
        """Power through a node: what arrives over its incoming connections, or what leaves
        over its outgoing ones when nothing feeds it (or it is a transfer switch)."""
        lines = self._in_lines.get(node) if self._kind[node] != ATS else None
        if lines:
            return float(-self._line_p[lines, 2].sum()), float(-self._line_p[lines, 3].sum())
        out = self._out_lines.get(node, [])
        return float(self._line_p[out, 0].sum()), float(self._line_p[out, 1].sum())

    def signals(self) -> dict[str, dict[str, float | bool]]:
        """True state per electrical asset (and `room:<id>` load) in SI units."""
        result: dict[str, dict[str, float | bool]] = {}
        switch = self._solved.switch if self._solved is not None else self._state().switch
        for node, kind in self._kind.items():
            faults = self._faults_of(node)
            vm = self.supply(node)
            s: dict[str, float | bool] = {"V_pu": vm, "energised": vm >= ENERGISED_PU}
            if kind in (UTILITY, GENSET, UPS):
                p, q = (float(v) for v in self._ext_pq[self._ext_of[node]])
            elif kind == LOAD:
                if node in self._cords:
                    rows = [self._load_of[f"{node}#cord{i}"] for i in range(len(self._cords[node]))]
                    p, q = (float(v) for v in self._load_pq[rows].sum(axis=0))
                else:
                    p, q = (float(v) for v in self._load_pq[self._load_of[node]])
                s["demand"] = self._demand[node]
            else:
                p, q = self._flow(node)
            s["P"], s["Q"] = p, q
            s["I"] = math.hypot(p, q) / (SQRT3 * V_LV_KV * 1e3 * vm) if vm > 1e-3 else 0.0
            if kind == UTILITY:
                s["available"] = self._supplies[node].available(self._utility, faults)
            elif kind == GENSET:
                g = self._gensets[node]
                s |= {
                    "running": g.running,
                    "starting": g.starting,
                    "locked_out": g.locked_out,
                    "start_cmd": g.start_cmd,
                    "P_available": g.available_w(faults),
                }
            elif kind == UPS:
                u = self._ups[node]
                bus_in = self._ups_buses[node][0]
                p_in, q_in = (float(v) for v in self._load_pq[self._load_of[f"{node}#in"]])
                s |= {
                    "V_in_pu": float(self._vm[bus_in]),
                    "P_in": p_in,
                    "Q_in": q_in,
                    "P_charge": u.charge_w(faults),
                    "soc": u.soc(faults),
                    "energy": u.energy_j,
                    "on_battery": u.on_battery,
                    "rectifier_on": u.rectifier_on,
                    "inverter_on": u.inverter_on,
                }
            elif kind == ATS:
                normal, emergency, _ = self._ats_buses[node]
                closed = [bool(switch[i]) for i in self._ats_switches[node]]
                s |= {
                    "source": float(POSITION_NORMAL if closed[0] else 0)
                    + float(POSITION_EMERGENCY if closed[1] else 0),
                    "normal_closed": closed[0],
                    "emergency_closed": closed[1],
                    "V_normal_pu": float(self._vm[normal]),
                    "V_emergency_pu": float(self._vm[emergency]),
                }
            elif kind == FEEDER:
                f = self._feeders[node]
                breaker = all(bool(switch[i]) for i in self._feeder_switches[node])
                s |= {"closed": breaker, "tripped": f.tripped}
            result[node] = s
        return result

    # --- lifecycle ---------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """JSON-able state keyed by World Model identity, so it survives a rebuild."""
        solved = self._solved
        return {
            "time": self._time,
            "utility": self._utility,
            "demand": dict(self._demand),
            "faults": {
                a: {m: dict(p) for m, p in modes.items()} for a, modes in self._faults.items()
            },
            "feeders": {n: f.snapshot() for n, f in self._feeders.items()},
            "gensets": {n: g.snapshot() for n, g in self._gensets.items()},
            "ups": {n: u.snapshot() for n, u in self._ups.items()},
            "ats": {n: a.snapshot() for n, a in self._ats.items()},
            "solved": None
            if solved is None
            else {
                "ext": dict(zip(self._ext_names, solved.ext.tolist(), strict=True)),
                "switch": dict(zip(self._switch_names, solved.switch.tolist(), strict=True)),
                "load": dict(zip(self._load_names, solved.load.tolist(), strict=True)),
            },
        }

    def restore(self, state: Mapping[str, Any]) -> None:
        """Load a snapshot. Assets absent from this network are ignored; the last solution is
        recomputed from the inputs it was solved with, so later steps match the original."""
        self._time = float(state["time"])
        self._utility = bool(state["utility"])
        for name, p in state["demand"].items():
            if name in self._demand:
                self._demand[name] = float(p)
        self._faults = {
            a: {m: {k: float(v) for k, v in p.items()} for m, p in modes.items()}
            for a, modes in state["faults"].items()
            if a in self.assets
        }
        tables: tuple[tuple[str, Mapping[str, Any]], ...] = (
            ("feeders", self._feeders),
            ("gensets", self._gensets),
            ("ups", self._ups),
            ("ats", self._ats),
        )
        for key, table in tables:
            for name, item in state[key].items():
                if name in table:
                    table[name].restore(item)
        current = self._state()
        solved = state.get("solved")
        if solved is not None:
            for names, values, stored in (
                (self._ext_names, current.ext, solved["ext"]),
                (self._switch_names, current.switch, solved["switch"]),
                (self._load_names, current.load, solved["load"]),
            ):
                for i, name in enumerate(names):
                    if name in stored:
                        values[i] = stored[name]
        ups_out = {n: u.p_out_w for n, u in self._ups.items()}
        self._solve(current)
        for name, p in ups_out.items():
            self._ups[name].p_out_w = p

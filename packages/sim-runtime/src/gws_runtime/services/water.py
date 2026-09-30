"""The cold water system: storage tanks, transfer and booster pumps, and the makeup pumps that
feed the cooling towers and the chilled water loop.

It is a mass balance, not a hydraulic network (ADR-0002 Amendment 4). The World Model's water
connections give the topology: every pump draws from what lies upstream of it through valves
(a tank, another pump's header, or the municipal main where a valve has nothing upstream) and
delivers to what lies downstream (a tank, another pump's suction, or a consumer). Each step:

- **Consumers draw what the plant uses.** A cooling tower evaporates its heat rejection
  (`Q / h_fg`) and bleeds off blowdown to hold its cycles of concentration, so its makeup is
  `evaporation x cycles / (cycles - 1)`. A closed chilled water loop draws nothing.
- **Demand-driven pumps deliver what lies downstream asks for.** Pumps that hold a discharge
  pressure (a set point, or a header feeding other pumps) keep their lead running; others run
  only on demand. A pump whose suction is dry (an empty tank, a closed valve, a header whose
  own pumps have stopped) stops on its low-suction protection and alarms.
- **Level-controlled pumps fill tanks.** Pumps that deliver into tanks run on the lowest of
  those tanks' levels, with hysteresis around the tanks' normal level, and bring in the lag
  pump near the low-level alarm.
- **The main refills its tanks** through a float valve that opens as a tank falls below its
  normal level, while the main is available (an operating condition).
- **Tanks integrate** their inflow, outflow and any leak (which drains in proportion to the
  level).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any

from gws_runtime.services.common import WATER_KG_M3, Env, Faults, Signals, number, param
from gws_world_model.model import Domain, WorldModel

MAINS = "mains"
PUMP, VALVE = "GwsLib.Pump", "GwsLib.Valve"
H_FG = 2.43e6
"""Latent heat of evaporation of water at tower conditions, J/kg."""
CYCLES = 4.0
"""Cycles of concentration the tower blowdown holds."""
EMPTY = 0.01
"""A tank at or below this fraction of full cannot feed a pump."""
BAND = 0.03
"""Level control hysteresis either side of a tank's normal level."""
FLOAT_BAND = 0.02
"""Level below normal at which the main's float valve is fully open."""
PUMP_EFFICIENCY = 0.65
MIN_SPEED = 0.3
NOMINAL_HZ = 50.0
MAINS_FLOW = 16.0
"""Flow the main delivers into one tank through a fully open float valve, kg/s."""


@dataclass(slots=True)
class _Path:
    terminal: str
    """A tank, pump or consumer asset id, or `mains`."""
    valves: frozenset[str]


@dataclass(slots=True)
class _Pump:
    asset: str
    m_nominal: float
    dp_nominal: float
    p_set: float | None
    """Discharge pressure set point (Pa) of a pump that holds one."""
    v_trip: float
    sources: list[_Path]
    dests: list[_Path]
    running: bool = False
    flow: float = 0.0
    speed: float = 0.0
    low_suction: bool = False
    run_s: float = 0.0
    manual: bool | None = None
    """An operator's start (True) or stop (False); None in auto."""

    @property
    def p_nominal_w(self) -> float:
        return self.m_nominal / WATER_KG_M3 * self.dp_nominal / PUMP_EFFICIENCY


@dataclass(slots=True)
class _Tank:
    asset: str
    capacity_kg: float
    normal: float
    alarm_low: float
    alarm_high: float
    level: float
    inlets: list[_Path]
    inflow: float = 0.0
    outflow: float = 0.0


@dataclass(slots=True)
class _Group:
    """Pumps that share their suction and their delivery: a duty/standby set."""

    pumps: list[str]
    level_controlled: bool
    holds_pressure: bool
    on: bool = False
    demand: float = 0.0


@dataclass
class WaterSystem:
    pumps: dict[str, _Pump]
    tanks: dict[str, _Tank]
    valves: dict[str, float]
    """Valve -> commanded position (0 closed .. 1 open)."""
    consumers: dict[str, str]
    """Consumer asset -> what it draws for (`tower` or `closed_loop`)."""
    groups: list[_Group]
    order: list[int]
    """Group indices downstream-first."""
    faults: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    stuck: dict[str, float] = field(default_factory=dict)
    mains_available: bool = True

    # --- construction ------------------------------------------------------------------------

    @classmethod
    def from_world(cls, doc: WorldModel, scope: Collection[str]) -> WaterSystem:
        up: dict[str, list[str]] = defaultdict(list)
        down: dict[str, list[str]] = defaultdict(list)
        nodes: set[str] = set()
        for c in sorted(doc.connections.values(), key=lambda c: c.id):
            if c.domain is not Domain.WATER or c.source.is_room or c.target.is_room:
                continue
            a, b = c.source.node, c.target.node
            if a not in scope or b not in scope:
                continue
            down[a].append(b)
            up[b].append(a)
            nodes |= {a, b}

        def behaviour(asset: str) -> str | None:
            return doc.component_types[doc.assets[asset].type].behaviour

        # Water equipment the site data draws unconnected still reports its own state.
        nodes |= {a for a in scope if _water_pump(doc, a) and behaviour(a) in (PUMP, VALVE)}

        pumps_ids = sorted(n for n in nodes if behaviour(n) == PUMP and _water_pump(doc, n))
        valve_ids = sorted(n for n in nodes if behaviour(n) == VALVE and _water_pump(doc, n))
        tank_ids = sorted(
            n
            for n in nodes
            if behaviour(n) is None
            and "volume" in doc.component_types[doc.assets[n].type].parameters
        )
        kinds = set(pumps_ids) | set(valve_ids) | set(tank_ids)

        def walk(start: str, links: Mapping[str, list[str]]) -> list[_Path]:
            out: list[_Path] = []
            stack: list[tuple[str, frozenset[str]]] = [
                (n, frozenset()) for n in links.get(start, [])
            ]
            if not stack and links is up and start in valve_ids:
                return [_Path(MAINS, frozenset())]
            seen: set[tuple[str, frozenset[str]]] = set()
            while stack:
                node, via = stack.pop()
                if (node, via) in seen:
                    continue
                seen.add((node, via))
                if node in valve_ids:
                    nxt = links.get(node, [])
                    if not nxt and links is up:
                        out.append(_Path(MAINS, via | {node}))
                    stack.extend((n, via | {node}) for n in nxt)
                else:
                    out.append(_Path(node, via))
            return sorted(out, key=lambda p: (p.terminal, sorted(p.valves)))

        pumps: dict[str, _Pump] = {}
        for p in pumps_ids:
            p_set = param(doc, p, "discharge_pressure_set", -1.0)
            pumps[p] = _Pump(
                asset=p,
                m_nominal=param(doc, p, "m_flow_nominal", 1.0),
                dp_nominal=param(doc, p, "dp_nominal", 300.0) * 1e3,
                p_set=p_set * 1e3 if p_set >= 0 else None,
                v_trip=param(doc, p, "v_trip_pu", 0.85),
                sources=walk(p, up),
                dests=walk(p, down),
            )
        tanks: dict[str, _Tank] = {}
        for t in tank_ids:
            tanks[t] = _Tank(
                asset=t,
                capacity_kg=param(doc, t, "volume", 100.0) * WATER_KG_M3,
                normal=param(doc, t, "level_start", 0.5),
                alarm_low=param(doc, t, "level_alarm_low", 0.2),
                alarm_high=param(doc, t, "level_alarm_high", 0.95),
                level=param(doc, t, "level_start", 0.5),
                inlets=walk(t, up),
            )
        consumers: dict[str, str] = {}
        for n in sorted(nodes - kinds):
            consumers[n] = "tower" if behaviour(n) == "GwsLib.CoolingTower" else "closed_loop"

        by_key: dict[tuple[tuple[str, ...], tuple[str, ...]], list[str]] = {}
        for pump in pumps.values():
            key = (
                tuple(sorted({s.terminal for s in pump.sources})),
                tuple(sorted({d.terminal for d in pump.dests})),
            )
            by_key.setdefault(key, []).append(pump.asset)
        groups: list[_Group] = []
        for (_, dests), members in sorted(by_key.items()):
            level = any(d in tanks for d in dests)
            holds = any(pumps[m].p_set is not None for m in members) or any(
                d in pumps for d in dests
            )
            groups.append(
                _Group(pumps=sorted(members), level_controlled=level, holds_pressure=holds)
            )
        order = _downstream_first(groups, pumps)
        valves = {v: param(doc, v, "position", 1.0) for v in valve_ids}
        return cls(pumps, tanks, valves, consumers, groups, order)

    @property
    def assets(self) -> set[str]:
        return set(self.pumps) | set(self.tanks) | set(self.valves)

    # --- faults and commands -----------------------------------------------------------------

    def fault(self, asset: str, mode: str, values: Mapping[str, float]) -> None:
        if mode == "stuck" and asset in self.valves and asset not in self.stuck:
            self.stuck[asset] = self.valves[asset]
        self.faults.setdefault(asset, {})[mode] = dict(values)

    def clear(self, asset: str, mode: str) -> None:
        self.faults.get(asset, {}).pop(mode, None)
        if mode == "stuck":
            self.stuck.pop(asset, None)

    def command(self, asset: str, signal: str, value: float | bool) -> bool:
        if asset in self.valves and signal in ("position", "On_Off"):
            self.valves[asset] = min(max(float(value), 0.0), 1.0)
            return True
        pump = self.pumps.get(asset)
        if pump is None:
            return False
        if signal in ("start", "stop"):
            pump.manual = signal == "start"
        elif signal == "auto":
            pump.manual = None
        elif signal == "p_set" and pump.p_set is not None:
            pump.p_set = float(value)
        else:
            return False
        return True

    def _faults(self, asset: str) -> Faults:
        return self.faults.get(asset, {})

    def _open(self, valve: str) -> bool:
        return self.stuck.get(valve, self.valves[valve]) >= 0.5

    def _path_open(self, path: _Path) -> bool:
        return all(self._open(v) for v in path.valves)

    # --- stepping ----------------------------------------------------------------------------

    def _available(self, pump: _Pump, env: Env) -> bool:
        if "trip" in self._faults(pump.asset) or pump.manual is False:
            return False
        return env.supply(pump.asset) >= pump.v_trip

    def _consumer_demand(self, asset: str, env: Env) -> float:
        if self.consumers.get(asset) != "tower":
            return 0.0
        s = env.state.get(asset, {})
        m, t_in, t_out = number(s, "m_flow"), number(s, "TEnt"), number(s, "TLvg")
        if m is None or t_in is None or t_out is None:
            return 0.0
        q = max(m, 0.0) * 4184.0 * max(t_in - t_out, 0.0)
        return q / H_FG * CYCLES / (CYCLES - 1)

    def _wet(self, pump: _Pump, wet_pumps: set[str]) -> bool:
        for src in pump.sources:
            if not self._path_open(src):
                continue
            if src.terminal == MAINS and self.mains_available:
                return True
            tank = self.tanks.get(src.terminal)
            if tank is not None and tank.level > EMPTY:
                return True
            if src.terminal in wet_pumps:
                return True
        return False

    def step(self, env: Env, mains_available: bool) -> None:
        self.mains_available = mains_available
        dt = env.dt
        for tank in self.tanks.values():
            tank.inflow = tank.outflow = 0.0
        demand: dict[str, float] = {
            c: self._consumer_demand(c, env) for c in sorted(self.consumers)
        }
        available = {p: self._available(pump, env) for p, pump in self.pumps.items()}
        # Suction: upstream-first, so a header is wet only while a pump feeding it runs.
        delivering: set[str] = set()
        for index in reversed(self.order):
            for p in self.groups[index].pumps:
                if available[p] and self._wet(self.pumps[p], delivering):
                    delivering.add(p)
        # Demand-driven groups, downstream-first, pass their draw upstream.
        pulled: dict[str, float] = defaultdict(float)
        for index in self.order:
            group = self.groups[index]
            if group.level_controlled:
                continue
            want = pulled[f"group:{index}"]
            for p in group.pumps:
                for d in self.pumps[p].dests:
                    if d.terminal in demand and self._path_open(d):
                        want += demand[d.terminal] / self._feeders(d.terminal)
            group.demand = want
            self._run(group, want, available, delivering, pulled, env)
        for index in self.order:
            group = self.groups[index]
            if group.level_controlled:
                self._level_control(group, available, delivering, pulled, env)
        self._mains(env)
        for tank in self.tanks.values():
            leak = self._faults(tank.asset).get("leak")
            leak_kg_s = leak.get("leak_flow", 0.0) * tank.level if leak is not None else 0.0
            net = tank.inflow - tank.outflow - leak_kg_s
            tank.level = min(max(tank.level + net * dt / tank.capacity_kg, 0.0), 1.0)
        for pump in self.pumps.values():
            if pump.running:
                pump.run_s += dt

    def _feeders(self, terminal: str) -> int:
        return max(
            sum(1 for p in self.pumps.values() if any(d.terminal == terminal for d in p.dests)), 1
        )

    def _draw(self, pump: _Pump, flow: float, pulled: dict[str, float]) -> None:
        """Take a pump's flow from its sources: tanks, the main, or upstream pump headers."""
        tanks = [
            s.terminal
            for s in pump.sources
            if s.terminal in self.tanks
            and self._path_open(s)
            and self.tanks[s.terminal].level > EMPTY
        ]
        headers = [
            s.terminal for s in pump.sources if s.terminal in self.pumps and self._path_open(s)
        ]
        if tanks:
            for t in tanks:
                self.tanks[t].outflow += flow / len(tanks)
        elif headers:
            groups = sorted({self._group_of(h) for h in headers})
            for g in groups:
                pulled[f"group:{g}"] += flow / len(groups)

    def _group_of(self, pump: str) -> int:
        return next(i for i, g in enumerate(self.groups) if pump in g.pumps)

    def _run(
        self,
        group: _Group,
        want: float,
        available: Mapping[str, bool],
        delivering: set[str],
        pulled: dict[str, float],
        env: Env,
    ) -> None:
        cap = max((self.pumps[p].m_nominal for p in group.pumps), default=1.0)
        n = math.ceil(want / cap - 1e-9) if want > 0 else 0
        if group.holds_pressure:
            n = max(n, 1)
        wet = self._call(group, n, available, delivering)
        flow = min(want, sum(self._capacity(p) for p in wet))
        for p in group.pumps:
            pump = self.pumps[p]
            pump.flow = flow / len(wet) if p in wet else 0.0
            pump.speed = max(MIN_SPEED, min(pump.flow / pump.m_nominal, 1.0)) if p in wet else 0.0
            if pump.running and pump.p_set is None:
                pump.speed = 1.0 if p in wet else 0.0
            if pump.flow > 0:
                self._draw(pump, pump.flow, pulled)
        for p in group.pumps:
            for d in self.pumps[p].dests:
                if d.terminal in self.tanks and self._path_open(d):
                    self.tanks[d.terminal].inflow += self.pumps[p].flow

    def _call(
        self, group: _Group, n: int, available: Mapping[str, bool], delivering: set[str]
    ) -> list[str]:
        """Call `n` of a group's available pumps to run (wet ones first) plus any an operator
        started. A called pump with a dry suction stops on its low-suction protection."""
        ready = sorted((p for p in group.pumps if available[p]), key=lambda p: p not in delivering)
        called = set(ready[:n]) | {
            p for p in group.pumps if self.pumps[p].manual is True and available[p]
        }
        for p in group.pumps:
            pump = self.pumps[p]
            pump.low_suction = p in called and p not in delivering
            pump.running = p in called and p in delivering
        return sorted(p for p in called if p in delivering)

    def _capacity(self, pump: str) -> float:
        wear = self._faults(pump).get("impeller_wear")
        return self.pumps[pump].m_nominal * (wear.get("head_fraction", 1.0) if wear else 1.0)

    def _level_control(
        self,
        group: _Group,
        available: Mapping[str, bool],
        delivering: set[str],
        pulled: dict[str, float],
        env: Env,
    ) -> None:
        dests = {
            d.terminal
            for p in group.pumps
            for d in self.pumps[p].dests
            if d.terminal in self.tanks and self._path_open(d)
        }
        if dests:
            low = min(self.tanks[t].level for t in dests)
            normal = min(self.tanks[t].normal for t in dests)
            alarm = max(self.tanks[t].alarm_low for t in dests)
            if low < normal - BAND:
                group.on = True
            elif low > normal + BAND:
                group.on = False
            n = (2 if low < alarm + FLOAT_BAND else 1) if group.on else 0
        else:
            low, n = 1.0, 0
        wet = self._call(group, n, available, delivering)
        for p in group.pumps:
            pump = self.pumps[p]
            pump.flow = self._capacity(p) if p in wet and dests else 0.0
            pump.speed = 1.0 if pump.flow > 0 else 0.0
            if pump.flow > 0:
                self._draw(pump, pump.flow, pulled)
                for t in dests:
                    self.tanks[t].inflow += pump.flow / len(dests)

    def _mains(self, env: Env) -> None:
        if not self.mains_available:
            return
        for tank in self.tanks.values():
            for inlet in tank.inlets:
                if inlet.terminal == MAINS and self._path_open(inlet):
                    opening = min(max((tank.normal - tank.level) / FLOAT_BAND, 0.0), 1.0)
                    cap = min((self.valves[v] for v in inlet.valves), default=1.0)
                    tank.inflow += opening * MAINS_FLOW * cap
                    break

    # --- outputs -----------------------------------------------------------------------------

    def demand_w(self, asset: str) -> float:
        pump = self.pumps.get(asset)
        if pump is None or not pump.running:
            return 0.0
        if pump.p_set is not None:
            return pump.p_nominal_w * max(pump.speed, MIN_SPEED) ** 3
        return pump.p_nominal_w

    def signals(self, env: Env) -> dict[str, Signals]:
        out: dict[str, Signals] = {}
        for p, pump in self.pumps.items():
            tripped = "trip" in self._faults(p)
            energised = env.supply(p) >= pump.v_trip
            header = pump.p_set if pump.p_set is not None else pump.dp_nominal
            out[p] = {
                "running": pump.running,
                "energised": energised,
                "tripped": tripped,
                "low_suction": pump.low_suction,
                "alarm": tripped or pump.low_suction,
                "m_flow": pump.flow,
                "speed": pump.speed,
                "f_out": pump.speed * NOMINAL_HZ if pump.running else 0.0,
                "p_dis": header if pump.flow > 0 else 0.0,
                "p_set": header,
                "run_s": pump.run_s,
            }
        for t, tank in self.tanks.items():
            alarm = not tank.alarm_low <= tank.level <= tank.alarm_high
            out[t] = {
                "level": tank.level,
                "level_alarm": alarm,
                "alarm": alarm,
                "inflow": tank.inflow,
                "outflow": tank.outflow,
            }
        for v in self.valves:
            out[v] = {"open": self._open(v), "position": self.stuck.get(v, self.valves[v])}
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "pumps": {
                p: {"run_s": x.run_s, "manual": x.manual, "p_set": x.p_set, "running": x.running}
                for p, x in self.pumps.items()
            },
            "tanks": {t: x.level for t, x in self.tanks.items()},
            "valves": dict(self.valves),
            "stuck": dict(self.stuck),
            "groups": [g.on for g in self.groups],
            "faults": {a: {m: dict(v) for m, v in f.items()} for a, f in self.faults.items()},
        }

    def restore(self, s: Mapping[str, Any]) -> None:
        for p, x in s["pumps"].items():
            if p not in self.pumps:
                continue
            pump = self.pumps[p]
            pump.run_s, pump.manual, pump.running = float(x["run_s"]), x["manual"], x["running"]
            pump.p_set = None if x["p_set"] is None else float(x["p_set"])
        for t, level in s["tanks"].items():
            if t in self.tanks:
                self.tanks[t].level = float(level)
        self.valves |= {k: float(v) for k, v in s["valves"].items() if k in self.valves}
        self.stuck = {k: float(v) for k, v in s["stuck"].items() if k in self.valves}
        if len(s["groups"]) == len(self.groups):
            for g, on in zip(self.groups, s["groups"], strict=True):
                g.on = bool(on)
        self.faults = {a: {m: dict(v) for m, v in f.items()} for a, f in s["faults"].items()}


def _water_pump(doc: WorldModel, asset: str) -> bool:
    """Whether a pump or valve is on the cold water system: both its passage ends are water
    (a chilled water pump's makeup connection does not make it one)."""
    ports = doc.component_types[doc.assets[asset].type].ports
    ends = [ports.get("water_in"), ports.get("water_out")]
    return all(p is not None and p.domain is Domain.WATER for p in ends)


def _downstream_first(groups: list[_Group], pumps: Mapping[str, _Pump]) -> list[int]:
    """Group indices ordered so a group comes before every group it draws from."""
    index = {p: i for i, g in enumerate(groups) for p in g.pumps}
    upstream: dict[int, set[int]] = {i: set() for i in range(len(groups))}
    for i, g in enumerate(groups):
        for p in g.pumps:
            for s in pumps[p].sources:
                if s.terminal in index:
                    upstream[i].add(index[s.terminal])
    depth: dict[int, int] = {}

    def level(i: int, seen: frozenset[int] = frozenset()) -> int:
        if i in depth:
            return depth[i]
        if i in seen:
            return 0
        d = 1 + max((level(u, seen | {i}) for u in upstream[i]), default=-1)
        depth[i] = d
        return d

    return sorted(range(len(groups)), key=lambda i: (-level(i), i))

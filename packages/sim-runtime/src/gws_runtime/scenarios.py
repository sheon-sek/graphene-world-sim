"""Incident scenarios (#22): event logs of single and compound faults, for evaluating the agents
above Ignition (Phase 8).

A scenario is data (`data/scenarios/incidents.json`): the events to apply after the site has
settled, how long to watch, and the truth an analyst should reach. `run` plays one on a
runtime session; because a session's trajectory depends only on its World Model, scope, seed,
step and event log (ADR-0002), the same scenario always produces the same data, and
`Session.replay` reproduces it.

    uv run python -m gws_runtime.scenarios data/graphene data/scenarios/incidents.json

runs every scenario and prints what each one changes.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gws_runtime.compiler import CACHE
from gws_runtime.gate import IT_FRACTION, SLICE
from gws_runtime.lifecycle import Session
from gws_world_model.model import WorldModel

SETUP: tuple[tuple[str, dict[str, Any]], ...] = (
    ("conditions", {"changes": {"it_fraction": {"DH01": IT_FRACTION}}}),
)
"""Applied before the warm-up: the load the gate runs the slice at."""


@dataclass(frozen=True)
class ScenarioEvent:
    at_s: float
    """Seconds after the warm-up."""
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    events: tuple[ScenarioEvent, ...]
    observe_s: float
    truth: dict[str, Any]
    domains: tuple[str, ...] = ()
    compound: bool = False


@dataclass(frozen=True)
class ScenarioSet:
    dt: float
    warmup_s: float
    scenarios: tuple[Scenario, ...]

    @classmethod
    def read(cls, path: Path) -> ScenarioSet:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            dt=float(raw["dt"]),
            warmup_s=float(raw["warmup_s"]),
            scenarios=tuple(
                Scenario(
                    id=s["id"],
                    title=s["title"],
                    events=tuple(
                        ScenarioEvent(float(e["at_s"]), e["kind"], dict(e["payload"]))
                        for e in s["events"]
                    ),
                    observe_s=float(s["observe_s"]),
                    truth=dict(s["truth"]),
                    domains=tuple(s.get("domains", ())),
                    compound=bool(s.get("compound", False)),
                )
                for s in raw["scenarios"]
            ),
        )

    def get(self, scenario_id: str) -> Scenario:
        for s in self.scenarios:
            if s.id == scenario_id:
                return s
        raise KeyError(scenario_id)

    def steps(self, seconds: float) -> int:
        return round(seconds / self.dt)


def scope(doc: WorldModel) -> frozenset[str]:
    """Where scenarios run: the Phase 1 slice's thermofluid plant plus every asset without a
    thermofluid model (electrical, network, services, controllers, instruments). It compiles
    to the slice's one partition, so it runs fast and needs no new FMU, while every power and
    network fault reaches the points Ignition reads."""
    thermofluid = {
        a
        for a, asset in doc.assets.items()
        if (behaviour := doc.component_types[asset.type].behaviour or "").startswith("GwsLib.")
        and not behaviour.startswith("GwsLib.Electrical.")
    }
    return frozenset(SLICE) | (doc.assets.keys() - thermofluid)


def schedule(
    scenarios: ScenarioSet, scenario: Scenario
) -> Iterator[tuple[int, str, dict[str, Any]]]:
    """The scenario's events as (step, kind, payload), counted from the session's start."""
    start = scenarios.steps(scenarios.warmup_s)
    for kind, payload in SETUP:
        yield 0, kind, payload
    for e in sorted(scenario.events, key=lambda e: e.at_s):
        yield start + scenarios.steps(e.at_s), e.kind, e.payload


def run(
    doc: WorldModel,
    scenarios: ScenarioSet,
    scenario: Scenario,
    *,
    seed: int = 0,
    cache: Path = CACHE,
    until_s: float | None = None,
) -> Session:
    """Play a scenario from a fresh session to the end of its observation window (or to
    `until_s` after the warm-up)."""
    session = Session(doc, scope(doc), seed=seed, dt=scenarios.dt, cache=cache, revision=1)
    end = scenarios.steps(scenarios.warmup_s + (scenario.observe_s if until_s is None else until_s))
    pending = list(schedule(scenarios, scenario))
    while True:
        while pending and pending[0][0] == session.sim.step_count:
            _, kind, payload = pending.pop(0)
            session.apply(kind, payload)
        if session.sim.step_count >= end:
            return session
        next_at = pending[0][0] if pending else end
        session.step(max(min(next_at, end) - session.sim.step_count, 1))


def _changes(before: dict[str, Any], after: dict[str, Any], top: int = 12) -> list[str]:
    out: list[tuple[float, str]] = []
    for path, now in after.items():
        was = before.get(path)
        if was is None:
            continue
        if was["quality"] != now["quality"]:
            out.append(
                (1e9, f"{path}: {was['quality']} -> {now['quality']} {now.get('reason', '')}")
            )
            continue
        a, b = was["value"], now["value"]
        if isinstance(a, bool) or isinstance(b, bool) or isinstance(a, str) or isinstance(b, str):
            if a != b:
                out.append((1e6, f"{path}: {a} -> {b}"))
        elif isinstance(a, int | float) and isinstance(b, int | float) and a != b:
            rel = abs(b - a) / max(abs(a), 1e-6)
            if rel > 0.02:
                out.append((rel, f"{path}: {a:.4g} -> {b:.4g}"))
    return [text for _, text in sorted(out, reverse=True)[:top]]


def main(argv: list[str]) -> None:
    """Print, per scenario, the points that differ from a run of the same session without the
    scenario's events: with the same seed, everything else is identical, so what is left is
    what the incident caused."""
    from dataclasses import replace  # noqa: PLC0415

    from gws_world_model.importers.graphene import Sources, build  # noqa: PLC0415

    doc = build(Sources.read(Path(argv[0])))
    scenarios = ScenarioSet.read(Path(argv[1]))
    for scenario in scenarios.scenarios:
        if len(argv) > 2 and scenario.id not in argv[2:]:
            continue
        calm = run(doc, scenarios, replace(scenario, events=()))
        baseline = calm.sim.frame().to_json()["points"]
        calm.close()
        session = run(doc, scenarios, scenario)
        end = session.sim.frame().to_json()["points"]
        print(f"## {scenario.id}")
        for line in _changes(baseline, end, top=30):
            print("  " + line)
        session.close()


if __name__ == "__main__":
    main(sys.argv[1:])

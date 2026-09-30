"""Lifecycle and event log (ADR-0002): init, run, pause, speed, snapshot, restore, reinit and
replay of one runtime session.

Every input that changes the trajectory — an operator command, a fault, a clear, a reset, a
change of operating conditions, a reinit to another World Model revision — is an Event
recorded at the step it acts from. The trajectory depends only on the World Model revisions,
the scope, the seed, the macro step and the events, so replaying the log from the start
reproduces it exactly. Pause and speed only pace the run against the wall clock; they are
logged for the audit trail but never change what is computed.

A snapshot holds the complete runtime state and the log up to that point. Restoring it
continues from there on a new branch: events after the snapshot are dropped from the log.
Reinit rebuilds the models from another revision of the World Model (a structural edit) and
carries the state over by asset id.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gws_runtime.compiler import CACHE
from gws_runtime.master import Frame, RuntimeProblem, Simulation
from gws_world_model.model import WorldModel

TRAJECTORY_EVENTS = frozenset({"command", "fault", "clear", "reset", "conditions", "reinit"})


@dataclass(frozen=True, slots=True)
class Event:
    step: int
    """The event acts from this step on (it was applied after `step` steps had run)."""
    t: float
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"step": self.step, "t": self.t, "kind": self.kind, "payload": self.payload}


@dataclass
class Snapshot:
    id: str
    label: str
    state: dict[str, Any]
    events: list[Event]
    revision: int | None


class Session:
    """A runtime session: one Simulation and its event log."""

    def __init__(
        self,
        doc: WorldModel,
        scope: Collection[str],
        *,
        seed: int = 0,
        dt: float = 1.0,
        cache: Path = CACHE,
        revision: int | None = None,
        resolve: Callable[[int], WorldModel] | None = None,
    ) -> None:
        """`resolve` gives the World Model of a revision number, for reinit and replay."""
        self.initial = (doc, frozenset(scope), seed, float(dt), revision)
        self.cache = cache
        self.resolve = resolve
        self.revision = revision
        self.scope = frozenset(scope)
        self.sim = Simulation(doc, self.scope, seed=seed, dt=dt, cache=cache)
        self.events: list[Event] = []
        self.snapshots: dict[str, Snapshot] = {}
        self.running = False
        self.speed = 1.0
        """Simulated seconds per wall-clock second while running; 0 runs flat out."""
        self._log("init", {"scope": sorted(self.scope), "seed": seed, "dt": float(dt)})

    # --- log -----------------------------------------------------------------------------

    def _log(self, kind: str, payload: Mapping[str, Any]) -> Event:
        event = Event(self.sim.step_count, self.sim.t, kind, dict(payload))
        self.events.append(event)
        return event

    def _apply(self, kind: str, payload: Mapping[str, Any]) -> Any:
        sim = self.sim
        if kind == "command":
            sim.command(payload["target"], payload.get("signal"), payload["value"])
            return None
        if kind == "fault":
            fault = sim.inject(
                payload["target"],
                payload["mode"],
                payload.get("parameters"),
                severity=float(payload.get("severity", 1.0)),
                ramp_s=float(payload.get("ramp_s", 0.0)),
                duration_s=payload.get("duration_s"),
                fault_id=payload.get("id", ""),
            )
            return fault
        if kind == "clear":
            return sim.clear(payload["id"])
        if kind == "reset":
            return sim.reset(payload["target"])
        if kind == "conditions":
            sim.set_conditions(payload["changes"])
            return None
        if kind == "reinit":
            self._reinit(int(payload["revision"]), payload.get("scope"))
            return None
        raise RuntimeProblem(f"unknown event kind {kind!r}")

    def apply(self, kind: str, payload: Mapping[str, Any]) -> Any:
        """Apply a trajectory event now and record it. A fault is recorded with the id it was
        given, so a replay gives it the same one."""
        if kind not in TRAJECTORY_EVENTS:
            raise RuntimeProblem(f"unknown event kind {kind!r}")
        result = self._apply(kind, payload)
        recorded = dict(payload)
        if kind == "fault":
            recorded["id"] = result.id
        self._log(kind, recorded)
        return result

    # --- running -------------------------------------------------------------------------

    def step(self, steps: int = 1) -> Frame:
        return self.sim.run(steps)

    def run(self, speed: float | None = None) -> None:
        if speed is not None:
            self.set_speed(speed)
        self.running = True
        self._log("run", {"speed": self.speed})

    def pause(self) -> None:
        self.running = False
        self._log("pause", {})

    def set_speed(self, speed: float) -> None:
        if speed < 0:
            raise RuntimeProblem("speed cannot be negative")
        self.speed = float(speed)
        self._log("speed", {"speed": self.speed})

    # --- snapshots -----------------------------------------------------------------------

    def snapshot(self, label: str = "") -> Snapshot:
        snap = Snapshot(
            id=f"S{len(self.snapshots) + 1}",
            label=label,
            state=copy.deepcopy(self.sim.snapshot()),
            events=list(self.events),
            revision=self.revision,
        )
        self.snapshots[snap.id] = snap
        self._log("snapshot", {"id": snap.id, "label": label})
        return snap

    def restore(self, snapshot_id: str) -> Frame:
        snap = self.snapshots.get(snapshot_id)
        if snap is None:
            raise RuntimeProblem(f"no snapshot {snapshot_id!r}")
        if snap.revision != self.revision:
            self._rebuild(self._doc(snap.revision), self.scope)
            self.revision = snap.revision
        self.sim.restore(copy.deepcopy(snap.state))
        self.events = list(snap.events)
        self._log("restore", {"id": snapshot_id})
        return self.sim.frame()

    # --- reinit --------------------------------------------------------------------------

    def _doc(self, revision: int | None) -> WorldModel:
        if revision == self.initial[4] or revision is None:
            return self.initial[0]
        if self.resolve is None:
            raise RuntimeProblem("this session cannot load other World Model revisions")
        return self.resolve(revision)

    def _rebuild(self, doc: WorldModel, scope: Collection[str]) -> None:
        state = self.sim.snapshot()
        old = self.sim
        self.scope = frozenset(scope)
        self.sim = Simulation(
            doc, self.scope, seed=old.seed, dt=old.dt, cache=self.cache, start_time=old.t
        )
        old.close()
        self.sim.restore(state)

    def _reinit(self, revision: int, scope: Collection[str] | None) -> None:
        self._rebuild(self._doc(revision), scope if scope is not None else self.scope)
        self.revision = revision

    def reinit(self, revision: int, scope: Collection[str] | None = None) -> Frame:
        """Rebuild the models from another World Model revision, keeping the state of every
        asset that still exists."""
        payload: dict[str, Any] = {"revision": revision}
        if scope is not None:
            payload["scope"] = sorted(scope)
        self.apply("reinit", payload)
        return self.sim.frame()

    # --- replay --------------------------------------------------------------------------

    def replay(self, until_step: int | None = None) -> Session:
        """A new session that re-runs this one's log from the start to `until_step` (default:
        where this one is). Its trajectory is identical."""
        doc, scope, seed, dt, revision = self.initial
        target = self.sim.step_count if until_step is None else until_step
        other = Session(
            doc, scope, seed=seed, dt=dt, cache=self.cache, revision=revision, resolve=self.resolve
        )
        pending = [e for e in self.events if e.kind in TRAJECTORY_EVENTS and e.step <= target]
        i = 0
        while True:
            while i < len(pending) and pending[i].step == other.sim.step_count:
                other.apply(pending[i].kind, pending[i].payload)
                i += 1
            if other.sim.step_count >= target:
                break
            other.sim.step()
        return other

    def close(self) -> None:
        self.sim.close()

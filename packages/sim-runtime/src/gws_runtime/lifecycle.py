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
carries the state over by asset id. A swap does the same without stopping the run: the
partitions the edit changes compile in the background while the old models keep stepping,
and the swap happens between two steps. Partitions the edit leaves unchanged are taken over
running; only the changed ones start afresh, from the state carried over. If validation,
compilation or initialisation fails, the old models keep running and the swap reports why.
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gws_runtime.compiler import CACHE, cached, compile_partition, plan
from gws_runtime.master import Frame, RuntimeProblem, Simulation
from gws_world_model.model import WorldModel
from gws_world_model.validate import errors, validate

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


@dataclass
class Swap:
    """A structural edit being applied to a running session."""

    revision: int
    scope: frozenset[str]
    requested_t: float
    state: str = "compiling"
    """`compiling`, `ready` (compiled, waiting for the next step boundary), `applied` or
    `failed`."""
    reason: str = ""
    """Why it failed; the old models are still running."""
    compiling: tuple[str, ...] = ()
    """The partitions the edit changes, compiled in the background."""
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    applied_t: float | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "state": self.state,
            "reason": self.reason,
            "requested_t": self.requested_t,
            "applied_t": self.applied_t,
            "compiling": list(self.compiling),
            "added": list(self.added),
            "removed": list(self.removed),
        }


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
        self.swap: Swap | None = None
        """The latest structural edit applied without stopping, or being applied."""
        self._compiler: threading.Thread | None = None
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
        """Replace the simulation with one of `doc`, carrying the state over. Partitions both
        have are taken over running. If the new one cannot be built, the old one is kept."""
        state = self.sim.snapshot()
        old = self.sim
        new = Simulation(
            doc,
            frozenset(scope),
            seed=old.seed,
            dt=old.dt,
            cache=self.cache,
            start_time=old.t,
            adopt=old,
        )
        try:
            new.restore(state)
        except BaseException:
            new.detach(new.adopted)
            new.close()
            raise
        old.detach(new.adopted)
        old.close()
        self.sim, self.scope = new, frozenset(scope)

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

    # --- swap ----------------------------------------------------------------------------

    def next_scope(self, revision: int) -> frozenset[str]:
        """The scope a swap to `revision` keeps: the current one, less the assets the revision
        removes, plus the ones it adds."""
        before, after = self.sim.doc, self._doc(revision)
        return frozenset(
            (self.scope & after.assets.keys()) | (after.assets.keys() - before.assets.keys())
        )

    def prepare(self, revision: int, scope: Collection[str] | None = None) -> Swap:
        """Start applying another World Model revision without stopping: validate it, plan it
        and compile the partitions it changes in the background. `apply_swap` swaps it in
        once compiled. A swap already compiling is replaced."""
        doc = self._doc(revision)
        # Assets the revision removes leave the scope.
        chosen = (
            frozenset(scope) & doc.assets.keys() if scope is not None else self.next_scope(revision)
        )
        before = self.sim.doc.assets.keys()
        swap = Swap(
            revision,
            chosen,
            self.sim.t,
            added=tuple(sorted(doc.assets.keys() - before)),
            removed=tuple(sorted(before - doc.assets.keys())),
        )
        self.swap = swap
        problems = errors(validate(doc))
        if problems:
            swap.state = "failed"
            swap.reason = "; ".join(f"{i.path}: {i.message}" for i in problems[:5])
            return swap
        try:
            partitions = plan(doc, chosen).partitions
        except (RuntimeProblem, ValueError) as e:
            swap.state, swap.reason = "failed", str(e)
            return swap
        todo = [p for p in partitions if cached(p, self.cache) is None]
        swap.compiling = tuple(p.name for p in todo)
        if not todo:
            swap.state = "ready"
            return swap

        def work() -> None:
            try:
                for p in todo:
                    compile_partition(p, self.cache)
            except Exception as e:  # noqa: BLE001 - a failed compile is reported, not raised
                if self.swap is swap:
                    swap.state, swap.reason = "failed", str(e).strip() or type(e).__name__
                return
            if self.swap is swap and swap.state == "compiling":
                swap.state = "ready"

        self._compiler = threading.Thread(target=work, name="gws-swap-compile", daemon=True)
        self._compiler.start()
        return swap

    def wait_compiled(self, timeout: float | None = None) -> Swap | None:
        """Block until the swap being prepared is compiled (for tests and scripts)."""
        if self._compiler is not None:
            self._compiler.join(timeout)
        return self.swap

    def apply_swap(self) -> Frame | None:
        """At a step boundary: swap in the prepared revision if it is compiled. Returns the new
        frame, or None when there is nothing to swap or the swap failed (the old models keep
        running and `swap.reason` says why)."""
        swap = self.swap
        if swap is None or swap.state != "ready":
            return None
        try:
            self.apply("reinit", {"revision": swap.revision, "scope": sorted(swap.scope)})
        except Exception as e:  # noqa: BLE001 - initialisation failure rolls back
            swap.state, swap.reason = "failed", str(e).strip() or type(e).__name__
            return None
        swap.state, swap.applied_t = "applied", self.sim.t
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

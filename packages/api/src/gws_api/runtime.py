"""Runtime endpoints: sessions over a World Model revision, their lifecycle, commands, faults,
operating conditions, and the frame stream (ADR-0002).

A session simulates a scope of assets from one revision. Creating it compiles any partition
not already in the FMU cache, which can take minutes the first time. While a session runs,
a background task steps it, paced by its speed, and pushes every frame to the WebSocket
subscribers of `/stream`. Every call that changes the trajectory is recorded in the session's
event log, so `/replay` rebuilds the same trajectory.

Every frame a session produces is also kept in its History (the last two hours at a one second
step), which serves trends, the alarm log and the propagation timeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from gws_api.history import History
from gws_runtime import gate
from gws_runtime.compiler import CompileError
from gws_runtime.lifecycle import TRAJECTORY_EVENTS, Session
from gws_runtime.master import ELECTRICAL_UNITS, RuntimeProblem, Simulation
from gws_world_model.model import AssetSignal, InstrumentSource, PointClass
from gws_world_model.store import NotFound, SqliteStore

router = APIRouter(prefix="/runtime", tags=["runtime"])


class _Live:
    """A session with its runner task, lock, stream subscribers and history."""

    def __init__(self, sid: str, session: Session) -> None:
        self.id = sid
        self.session = session
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.history = History()
        self.last_error: dict[str, Any] | None = None
        self.created = time.monotonic()
        self._alarm_points: tuple[Simulation, dict[str, str | None]] | None = None
        self.record(session.sim.frame().to_json())

    def alarm_points(self) -> dict[str, str | None]:
        """The scope's fault-alarm points and the asset each belongs to."""
        sim = self.session.sim
        if self._alarm_points is None or self._alarm_points[0] is not sim:
            doc = sim.doc
            found: dict[str, str | None] = {}
            for path, b in doc.point_bindings.items():
                if b.point_class is not PointClass.FAULT_ALARM:
                    continue
                src = b.source
                asset = None
                if isinstance(src, AssetSignal):
                    asset = src.asset
                elif isinstance(src, InstrumentSource):
                    asset = doc.instruments[src.instrument].asset
                if asset in sim.scope:
                    found[path] = asset
            self._alarm_points = (sim, found)
        return self._alarm_points[1]

    def context(self, t: float) -> dict[str, Any] | None:
        """The latest trajectory event at or before `t`: what an alarm most likely follows."""
        for event in reversed(self.session.events):
            if event.t <= t and event.kind in TRAJECTORY_EVENTS:
                return event.to_json()
        return None

    def record(self, frame: dict[str, Any]) -> None:
        self.history.record(frame, self.alarm_points, self.context)

    def publish(self, frame: dict[str, Any]) -> None:
        self.record(frame)
        for q in list(self.subscribers):
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()  # a slow client gets the newest frames
            q.put_nowait(frame)


class Registry:
    def __init__(self) -> None:
        self.sessions: dict[str, _Live] = {}
        self._ids = itertools.count(1)

    def new_id(self) -> str:
        return f"R{next(self._ids)}"


def get_registry(request: Request) -> Registry:
    if not hasattr(request.app.state, "runtime"):
        request.app.state.runtime = Registry()
    registry: Registry = request.app.state.runtime
    return registry


def get_store(request: Request) -> SqliteStore:
    store: SqliteStore = request.app.state.store
    return store


Runtime = Annotated[Registry, Depends(get_registry)]
Store = Annotated[SqliteStore, Depends(get_store)]


class _In(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SessionIn(_In):
    scope: list[str] = Field(min_length=1, description="Asset ids to simulate.")
    revision: int | None = Field(default=None, ge=1, description="Default: the head.")
    seed: int = 0
    dt: float = Field(default=1.0, gt=0, le=60, description="Macro step, seconds.")


class SessionOut(BaseModel):
    id: str
    revision: int | None
    t: float
    step: int
    dt: float
    running: bool
    speed: float
    scope: list[str]
    partitions: list[str]
    not_modelled: dict[str, str]
    blocks: list[str]
    missing_blocks: list[str]
    faults: list[dict[str, Any]]


class StepIn(_In):
    steps: int = Field(default=1, ge=1, le=100_000)


class RunIn(_In):
    speed: float | None = Field(default=None, ge=0, description="0 runs as fast as possible.")


class CommandIn(_In):
    target: str = Field(description="Asset id with `signal`, or a writable point path alone.")
    signal: str | None = None
    value: float | bool


class FaultIn(_In):
    target: str = Field(description="Asset, instrument, point path or network element.")
    mode: str
    parameters: dict[str, float] = Field(default_factory=dict)
    severity: float = Field(default=1.0, ge=0, le=1)
    ramp_s: float = Field(default=0.0, ge=0)
    duration_s: float | None = Field(default=None, gt=0)


class ResetIn(_In):
    target: str


class ReinitIn(_In):
    revision: int | None = Field(default=None, ge=1, description="Default: the head.")
    scope: list[str] | None = None


class SnapshotIn(_In):
    label: str = ""


def _out(live: _Live) -> SessionOut:
    s = live.session
    sim = s.sim
    return SessionOut(
        id=live.id,
        revision=s.revision,
        t=sim.t,
        step=sim.step_count,
        dt=sim.dt,
        running=s.running,
        speed=s.speed,
        scope=sorted(s.scope),
        partitions=sorted(sim.partitions),
        not_modelled=dict(sim.plan.not_modelled),
        blocks=[b.binding.id for b in sim.blocks],
        missing_blocks=list(sim.missing_blocks),
        faults=[f.to_json() for f in sim.faults.active()],
    )


def _live(runtime: Registry, sid: str) -> _Live:
    live = runtime.sessions.get(sid)
    if live is None:
        raise HTTPException(404, f"no runtime session {sid!r}")
    return live


def _error(during: str, e: BaseException) -> dict[str, Any]:
    return {"during": during, "type": type(e).__name__, "message": str(e).strip("'\"")}


async def _do(live: _Live, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a session call off the event loop, one at a time, mapping runtime errors to 422.
    A model that fails to compile is kept as the session's last error, log excerpt included."""
    async with live.lock:
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except CompileError as e:
            live.last_error = _error(getattr(fn, "__name__", "call"), e)
            raise HTTPException(422, live.last_error["message"]) from e
        except (RuntimeProblem, KeyError, ValueError) as e:
            raise HTTPException(422, str(e).strip("'\"")) from e


async def _runner(live: _Live) -> None:
    """Step while running, paced so that simulated time advances `speed` times wall time. The
    pace is kept on average: a step that runs late is made up by the next ones not waiting,
    and the schedule restarts when the speed changes or the run falls behind by more than
    `MAX_LAG_STEPS` steps."""
    loop = asyncio.get_running_loop()
    session = live.session
    due, pace = loop.time(), session.speed
    while session.running:
        if session.speed != pace or loop.time() - due > MAX_LAG_STEPS * session.sim.dt / max(
            session.speed, 1e-9
        ):
            due, pace = loop.time(), session.speed
        async with live.lock:
            try:
                frame = await run_in_threadpool(session.step, 1)
            except Exception as e:  # noqa: BLE001 - a failed step stops the run, not the server
                session.running = False
                live.last_error = _error("step", e)
                return
        live.publish(frame.to_json())
        if session.speed > 0:
            due += session.sim.dt / session.speed
            await asyncio.sleep(max(due - loop.time(), 0.0))
        else:
            await asyncio.sleep(0)


def _steps(live: _Live, steps: int) -> dict[str, Any]:
    """Step `steps` times, keeping every frame in the history; the last one is returned for
    the caller to publish."""
    for _ in range(steps - 1):
        live.record(live.session.step(1).to_json())
    data: dict[str, Any] = live.session.step(1).to_json()
    return data


def _start(live: _Live) -> None:
    if live.task is None or live.task.done():
        live.task = asyncio.get_running_loop().create_task(_runner(live))


class Preset(BaseModel):
    id: str
    name: str
    description: str
    scope: list[str]
    dt: float
    room: str
    conditions: dict[str, Any]
    """Operating conditions to set after creating the session (`PUT .../conditions`)."""


MAX_LAG_STEPS = 10
"""A run further behind its schedule than this many steps stops trying to catch up."""

PRESETS = [
    Preset(
        id="dh01-slice",
        name="DH01 cooling slice",
        description=(
            "Chiller 1 with its legs, the buffer tanks, the secondary pump, cooling block 1, "
            "FCU1 and CCU-001, five towers and DH01's IT load at "
            f"{gate.IT_FRACTION:.0%} of design."
        ),
        scope=list(gate.SLICE),
        dt=gate.DT,
        room="DH01",
        conditions={"it_fraction": {"DH01": gate.IT_FRACTION}},
    )
]


@router.get("/presets", response_model=list[Preset])
def presets() -> list[Preset]:
    """Scopes worth simulating, with the step and conditions they were validated at."""
    return PRESETS


# --- sessions ----------------------------------------------------------------------------------


@router.post("/sessions", status_code=201, response_model=SessionOut)
async def create_session(body: SessionIn, runtime: Runtime, store: Store) -> SessionOut:
    revision = body.revision or store.head()
    if revision is None:
        raise HTTPException(409, "the World Model has no revision yet")
    try:
        doc = store.get(revision)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    unknown = sorted(set(body.scope) - set(doc.assets))
    if unknown:
        raise HTTPException(422, f"unknown assets in scope: {unknown[:10]}")
    try:
        session = await run_in_threadpool(
            Session,
            doc,
            body.scope,
            seed=body.seed,
            dt=body.dt,
            revision=revision,
            resolve=store.get,
        )
    except CompileError as e:
        raise HTTPException(422, str(e)) from e
    live = _Live(runtime.new_id(), session)
    runtime.sessions[live.id] = live
    return _out(live)


@router.get("/sessions", response_model=list[SessionOut])
def list_sessions(runtime: Runtime) -> list[SessionOut]:
    return [_out(live) for live in runtime.sessions.values()]


@router.get("/sessions/{sid}", response_model=SessionOut)
def get_session(sid: str, runtime: Runtime) -> SessionOut:
    return _out(_live(runtime, sid))


@router.delete("/sessions/{sid}", status_code=204)
async def delete_session(sid: str, runtime: Runtime, request: Request) -> None:
    live = _live(runtime, sid)
    bridge = getattr(request.app.state, "opcua", None)
    if bridge is not None and bridge.live is live:
        await bridge.attach(None)
    live.session.running = False
    if live.task is not None:
        await live.task
    async with live.lock:
        await run_in_threadpool(live.session.close)
    del runtime.sessions[sid]


# --- lifecycle ---------------------------------------------------------------------------------


@router.post("/sessions/{sid}/step")
async def step(
    sid: str, runtime: Runtime, body: Annotated[StepIn | None, Body()] = None
) -> dict[str, Any]:
    live = _live(runtime, sid)
    if live.session.running:
        raise HTTPException(409, "the session is running; pause it to step")
    data: dict[str, Any] = await _do(live, _steps, live, (body or StepIn()).steps)
    live.publish(data)
    return data


@router.post("/sessions/{sid}/run", response_model=SessionOut)
async def run(
    sid: str, runtime: Runtime, body: Annotated[RunIn | None, Body()] = None
) -> SessionOut:
    live = _live(runtime, sid)
    await _do(live, live.session.run, (body or RunIn()).speed)
    _start(live)
    return _out(live)


@router.post("/sessions/{sid}/pause", response_model=SessionOut)
async def pause(sid: str, runtime: Runtime) -> SessionOut:
    live = _live(runtime, sid)
    live.session.running = False
    if live.task is not None:
        await live.task
    await _do(live, live.session.pause)
    return _out(live)


@router.put("/sessions/{sid}/speed", response_model=SessionOut)
async def speed(sid: str, runtime: Runtime, body: RunIn) -> SessionOut:
    live = _live(runtime, sid)
    if body.speed is None:
        raise HTTPException(422, "speed is required")
    await _do(live, live.session.set_speed, body.speed)
    return _out(live)


@router.get("/sessions/{sid}/frame")
async def frame(sid: str, runtime: Runtime) -> dict[str, Any]:
    live = _live(runtime, sid)
    result: dict[str, Any] = (await _do(live, live.session.sim.frame)).to_json()
    return result


@router.get("/sessions/{sid}/events")
def events(sid: str, runtime: Runtime) -> list[dict[str, Any]]:
    return [e.to_json() for e in _live(runtime, sid).session.events]


@router.post("/sessions/{sid}/snapshots", status_code=201)
async def snapshot(
    sid: str, runtime: Runtime, body: Annotated[SnapshotIn | None, Body()] = None
) -> dict[str, Any]:
    live = _live(runtime, sid)
    snap = await _do(live, live.session.snapshot, (body or SnapshotIn()).label)
    return {"id": snap.id, "label": snap.label, "t": snap.state["t"], "step": snap.state["step"]}


@router.post("/sessions/{sid}/snapshots/{snap_id}/restore")
async def restore(sid: str, snap_id: str, runtime: Runtime) -> dict[str, Any]:
    live = _live(runtime, sid)
    if live.session.running:
        raise HTTPException(409, "the session is running; pause it to restore")
    result: dict[str, Any] = (await _do(live, live.session.restore, snap_id)).to_json()
    live.publish(result)
    return result


@router.post("/sessions/{sid}/reinit")
async def reinit(sid: str, body: ReinitIn, runtime: Runtime, store: Store) -> dict[str, Any]:
    live = _live(runtime, sid)
    if live.session.running:
        raise HTTPException(409, "the session is running; pause it to reinit")
    revision = body.revision or store.head()
    if revision is None:
        raise HTTPException(409, "the World Model has no revision yet")
    result: dict[str, Any] = (await _do(live, live.session.reinit, revision, body.scope)).to_json()
    live.publish(result)
    return result


@router.post("/sessions/{sid}/replay", status_code=201, response_model=SessionOut)
async def replay(sid: str, runtime: Runtime) -> SessionOut:
    """A new session that re-runs this one's event log from the start to where it is now."""
    live = _live(runtime, sid)
    other = await _do(live, live.session.replay)
    copy = _Live(runtime.new_id(), other)
    runtime.sessions[copy.id] = copy
    return _out(copy)


# --- inputs ------------------------------------------------------------------------------------


@router.post("/sessions/{sid}/commands", status_code=204)
async def command(sid: str, body: CommandIn, runtime: Runtime) -> None:
    live = _live(runtime, sid)
    await _do(live, live.session.apply, "command", body.model_dump())


@router.post("/sessions/{sid}/faults", status_code=201)
async def inject(sid: str, body: FaultIn, runtime: Runtime) -> dict[str, Any]:
    live = _live(runtime, sid)
    fault = await _do(live, live.session.apply, "fault", body.model_dump())
    result: dict[str, Any] = fault.to_json()
    return result


@router.delete("/sessions/{sid}/faults/{fault_id}")
async def clear(sid: str, fault_id: str, runtime: Runtime) -> dict[str, Any]:
    """Clear a fault's cause. A latching trip stays in effect until its target is reset."""
    live = _live(runtime, sid)
    fault = await _do(live, live.session.apply, "clear", {"id": fault_id})
    result: dict[str, Any] = fault.to_json()
    return result


@router.post("/sessions/{sid}/reset")
async def reset(sid: str, body: ResetIn, runtime: Runtime) -> dict[str, Any]:
    live = _live(runtime, sid)
    removed = await _do(live, live.session.apply, "reset", body.model_dump())
    return {"cleared": removed}


@router.put("/sessions/{sid}/conditions")
async def conditions(
    sid: str, runtime: Runtime, changes: Annotated[dict[str, Any], Body()]
) -> dict[str, Any]:
    """Change operating conditions: `dry_bulb_c`, `wet_bulb_c`, `relative_humidity`,
    `utility_available`, `it_fraction` ({room: fraction})."""
    live = _live(runtime, sid)
    await _do(live, live.session.apply, "conditions", {"changes": changes})
    result: dict[str, Any] = live.session.sim.conditions.snapshot()
    return result


# --- history, alarms, propagation, diagnostics ------------------------------------------------


class SeriesKey(_In):
    point: str | None = Field(default=None, description="A point path; or `asset` and `signal`.")
    asset: str | None = None
    signal: str | None = None


class HistoryIn(_In):
    series: list[SeriesKey] = Field(min_length=1, max_length=64)
    since: float | None = Field(default=None, description="Simulated seconds; default: all kept.")
    until: float | None = None
    max_points: int = Field(default=2000, ge=2, le=20_000)


@router.post("/sessions/{sid}/history")
def history(sid: str, body: HistoryIn, runtime: Runtime) -> dict[str, Any]:
    """Recorded values of points and state signals, thinned to `max_points` samples. A value is
    null where the series had none (not published yet, or not a number)."""
    live = _live(runtime, sid)
    keys: list[tuple[str, str, str]] = []
    for k in body.series:
        if k.point is not None:
            keys.append(("point", k.point, ""))
        elif k.asset is not None and k.signal is not None:
            keys.append(("state", k.asset, k.signal))
        else:
            raise HTTPException(422, "each series needs `point`, or `asset` and `signal`")
    result = live.history.series(keys, body.since, body.until, body.max_points)
    span = live.history.span
    result["span"] = list(span) if span else None
    return result


@router.get("/sessions/{sid}/alarms")
def alarms(
    sid: str, runtime: Runtime, state: Literal["all", "active"] = "all"
) -> list[dict[str, Any]]:
    """Alarms raised by the scope's fault-alarm points, newest first, each with the trajectory
    event that preceded it."""
    live = _live(runtime, sid)
    found = [a.to_json() for a in reversed(live.history.alarms)]
    return [a for a in found if a["active"]] if state == "active" else found


@router.get("/sessions/{sid}/propagation")
def propagation(
    sid: str, runtime: Runtime, since: float, until: float | None = None
) -> list[dict[str, Any]]:
    """The order in which the consequences of what happened at `since` reached each asset:
    every asset whose state moved beyond a threshold, at the first step it did."""
    live = _live(runtime, sid)
    sim = live.session.sim
    units: dict[tuple[str, str], str | None] = dict(sim.units)
    for kind, asset, signal in live.history.columns:
        if kind == "state" and (asset, signal) not in units:
            units[(asset, signal)] = ELECTRICAL_UNITS.get(signal)
    return live.history.propagation(since, units, until)


@router.get("/sessions/{sid}/diagnostics")
def diagnostics(sid: str, runtime: Runtime) -> dict[str, Any]:
    """How the session is doing: partitions and their solvers, compile and step times, the
    real-time factor, what the scope leaves out, and the last error."""
    live = _live(runtime, sid)
    sim = live.session.sim
    timings = sim.timings.to_json()
    mean = timings["mean"].get("total")
    return {
        "id": live.id,
        "t": sim.t,
        "step": sim.step_count,
        "dt": sim.dt,
        "running": live.session.running,
        "speed": live.session.speed,
        "real_time_factor": sim.dt / mean if mean else None,
        "partitions": [
            {
                "name": name,
                "assets": sorted(part.assets),
                "rooms": sorted(part.rooms),
                "compile_seconds": sim.compile_seconds.get(name),
                "solver_steps": getattr(sim.fmus.get(name), "solver_steps", None),
                "events": getattr(sim.fmus.get(name), "events", None),
                "step_seconds": timings["mean"].get(name),
            }
            for name, part in sorted(sim.partitions.items())
        ],
        "timings": timings,
        "not_modelled": dict(sim.plan.not_modelled),
        "missing_blocks": list(sim.missing_blocks),
        "history": {
            "frames": min(live.history.count, live.history.capacity),
            "span": live.history.span,
        },
        "subscribers": len(live.subscribers),
        "last_error": live.last_error,
    }


# --- stream ------------------------------------------------------------------------------------


@router.websocket("/sessions/{sid}/stream")
async def stream(websocket: WebSocket, sid: str) -> None:
    """Every frame the session produces, as JSON, starting with the current one."""
    registry: Registry | None = getattr(websocket.app.state, "runtime", None)
    live = registry.sessions.get(sid) if registry is not None else None
    if live is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=32)
    live.subscribers.add(queue)
    try:
        await websocket.send_json(live.session.sim.frame().to_json())
        while True:
            await websocket.send_json(await queue.get())
    except WebSocketDisconnect:
        pass
    finally:
        live.subscribers.discard(queue)

"""Runtime endpoints: sessions over a World Model revision, their lifecycle, commands, faults,
operating conditions, and the frame stream (ADR-0002).

A session simulates a scope of assets from one revision. Creating it compiles any partition
not already in the FMU cache, which can take minutes the first time. While a session runs,
a background task steps it, paced by its speed, and pushes every frame to the WebSocket
subscribers of `/stream`. Every call that changes the trajectory is recorded in the session's
event log, so `/replay` rebuilds the same trajectory.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, WebSocket
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from starlette.websockets import WebSocketDisconnect

from gws_runtime.compiler import CompileError
from gws_runtime.lifecycle import Session
from gws_runtime.master import RuntimeProblem
from gws_world_model.store import NotFound, SqliteStore

router = APIRouter(prefix="/runtime", tags=["runtime"])


class _Live:
    """A session with its runner task, lock and stream subscribers."""

    def __init__(self, sid: str, session: Session) -> None:
        self.id = sid
        self.session = session
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def publish(self, frame: dict[str, Any]) -> None:
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


async def _do(live: _Live, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a session call off the event loop, one at a time, mapping runtime errors to 422."""
    async with live.lock:
        try:
            return await run_in_threadpool(fn, *args, **kwargs)
        except (RuntimeProblem, KeyError, ValueError) as e:
            raise HTTPException(422, str(e).strip("'\"")) from e


async def _runner(live: _Live) -> None:
    """Step while running, paced so that simulated time advances `speed` times wall time."""
    loop = asyncio.get_running_loop()
    session = live.session
    while session.running:
        began = loop.time()
        async with live.lock:
            frame = await run_in_threadpool(session.step, 1)
        live.publish(frame.to_json())
        if session.speed > 0:
            await asyncio.sleep(max(session.sim.dt / session.speed - (loop.time() - began), 0.0))
        else:
            await asyncio.sleep(0)


def _start(live: _Live) -> None:
    if live.task is None or live.task.done():
        live.task = asyncio.get_running_loop().create_task(_runner(live))


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
    frame = await _do(live, live.session.step, (body or StepIn()).steps)
    data: dict[str, Any] = frame.to_json()
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

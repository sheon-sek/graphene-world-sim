"""The OPC UA surface of the serving process (ADR-0001, ADR-0003).

`gws_opcua` knows nothing about the World Model or the runtime, so this module is where they
meet. The Bridge:

- builds the address space from the World Model's point bindings: the attached runtime
  session's World Model, or the store's head revision while no session is attached;
- follows the attached session's frames and publishes every point's value, quality and
  timestamp. A point outside the session's scope reads Bad `out_of_scope`;
- maps simulation time to SourceTimestamps: the session's current simulation time is the wall
  clock at the moment it is attached, and time then advances with the simulation;
- hands a write to a command point to the session as a runtime command, recorded in its event
  log exactly like a command from the web application;
- rebuilds the address space in place when the World Model changes (a reinit to another
  revision, a new head while no session is attached, or another session attached), so
  clients see new points through a model-change event without reconnecting.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from gws_api.runtime import Registry, _do, _Live, get_registry
from gws_opcua.points import PointSpec, PointValue, Scalar
from gws_opcua.server import PointServer
from gws_world_model.model import Access, WorldModel
from gws_world_model.store import SqliteStore

OUT_OF_SCOPE = "out_of_scope"
POLL_S = 1.0
"""How often the Bridge checks for a changed World Model when no frame arrives."""


def point_specs(doc: WorldModel) -> list[PointSpec]:
    """One point per point binding. Points the World Model marks read-write are commands."""
    return [
        PointSpec(
            path=b.path,
            data_type=b.data_type,
            writable=b.access is Access.READ_WRITE,
            unit=b.unit,
        )
        for b in sorted(doc.point_bindings.values(), key=lambda b: b.path)
    ]


class Bridge:
    """Feeds one PointServer from the store and at most one attached runtime session."""

    def __init__(self, server: PointServer, store: SqliteStore) -> None:
        self.server = server
        self.store = store
        self.live: _Live | None = None
        self.epoch = datetime.now(UTC)
        """Wall-clock time of simulation time 0 for the attached session."""
        self.doc: WorldModel | None = None
        self.revision: int | None = None
        self._from_session = False
        self._scoped: set[str] = set()
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        server.on_write = self.on_write
        server.clock = self.now

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        await self.server.start()
        async with self._lock:
            await self._sync_model()
        self._task = asyncio.get_running_loop().create_task(self._follow())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._unsubscribe()
        await self.server.stop()

    # --- time ------------------------------------------------------------------------------

    def timestamp(self, t: float) -> datetime:
        return self.epoch + timedelta(seconds=t)

    def now(self) -> datetime:
        """The attached session's current simulation time, as a SourceTimestamp."""
        if self.live is None:
            return datetime.now(UTC)
        return self.timestamp(self.live.session.sim.t)

    # --- attach ----------------------------------------------------------------------------

    async def attach(self, live: _Live | None) -> None:
        async with self._lock:
            self._unsubscribe()
            self.live = live
            if live is not None:
                self.epoch = datetime.now(UTC) - timedelta(seconds=live.session.sim.t)
                self._queue = asyncio.Queue(maxsize=4)
                live.subscribers.add(self._queue)
            await self._sync_model(force_values=True)
            if live is not None:
                frame = (await _do(live, live.session.sim.frame)).to_json()
                await self._publish(frame)

    def _unsubscribe(self) -> None:
        if self.live is not None and self._queue is not None:
            self.live.subscribers.discard(self._queue)
        self._queue = None

    # --- following -------------------------------------------------------------------------

    async def _follow(self) -> None:
        while True:
            frame: dict[str, Any] | None = None
            queue = self._queue
            if queue is not None:
                with contextlib.suppress(TimeoutError):
                    frame = await asyncio.wait_for(queue.get(), POLL_S)
            else:
                await asyncio.sleep(POLL_S)
            async with self._lock:
                if queue is not self._queue:
                    continue  # attached to another session meanwhile
                await self._sync_model()
                if frame is not None:
                    await self._publish(frame)

    async def _sync_model(self, *, force_values: bool = False) -> None:
        """Rebuild the address space if the World Model changed. Every point then reads out of
        scope until a frame gives it a value."""
        doc: WorldModel | None
        if self.live is None:
            head = self.store.head()
            changed = self._from_session or head != self.revision or self.doc is None
            doc = self.store.get(head) if changed and head is not None else self.doc
            revision = head
        else:
            doc, revision = self.live.session.sim.doc, self.live.session.revision
            changed = doc is not self.doc
        if changed:
            await self.server.set_points(point_specs(doc) if doc is not None else [])
            self.doc, self.revision = doc, revision
            self._from_session = self.live is not None
        if changed or force_values:
            self._scoped = set()
            stamp = self.now()
            await self.server.publish(
                {path: PointValue(None, "bad", stamp, OUT_OF_SCOPE) for path in self.server.specs}
            )

    async def _publish(self, frame: dict[str, Any]) -> None:
        points: dict[str, dict[str, Any]] = frame["points"]
        values = {
            path: PointValue(p["value"], p["quality"], self.timestamp(p["t"]), p.get("reason", ""))
            for path, p in points.items()
        }
        left = self._scoped - set(points)
        if left:
            stamp = self.timestamp(frame["t"])
            values.update({path: PointValue(None, "bad", stamp, OUT_OF_SCOPE) for path in left})
        self._scoped = set(points)
        await self.server.publish(values)

    # --- writes ----------------------------------------------------------------------------

    async def on_write(self, path: str, value: Scalar) -> str | None:
        live = self.live
        if live is None:
            return "no runtime session is attached to the OPC UA server"
        if isinstance(value, str | datetime):
            return f"{path} takes a number, not {value!r}"
        try:
            await _do(
                live,
                live.session.apply,
                "command",
                {"target": path, "signal": None, "value": value},
            )
        except HTTPException as e:
            return str(e.detail)
        return None


# --- API -------------------------------------------------------------------------------------

router = APIRouter(prefix="/opcua", tags=["opcua"])


def get_bridge(request: Request) -> Bridge:
    bridge: Bridge | None = getattr(request.app.state, "opcua", None)
    if bridge is None:
        raise HTTPException(404, "this server does not serve OPC UA")
    return bridge


OpcUa = Annotated[Bridge, Depends(get_bridge)]
Runtime = Annotated[Registry, Depends(get_registry)]


class OpcUaOut(BaseModel):
    endpoint: str
    session: str | None
    revision: int | None
    points: int
    writable: int


class AttachIn(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    session: str | None
    """Runtime session to serve; null detaches and serves the head revision, all out of scope."""


def _out(bridge: Bridge) -> OpcUaOut:
    specs = bridge.server.specs.values()
    return OpcUaOut(
        endpoint=bridge.server.endpoint,
        session=bridge.live.id if bridge.live is not None else None,
        revision=bridge.revision,
        points=len(specs),
        writable=sum(1 for s in specs if s.writable),
    )


@router.get("", response_model=OpcUaOut)
def status(bridge: OpcUa) -> OpcUaOut:
    return _out(bridge)


@router.put("/session", response_model=OpcUaOut)
async def attach(body: AttachIn, bridge: OpcUa, runtime: Runtime) -> OpcUaOut:
    """Serve this runtime session's points over OPC UA."""
    live = None
    if body.session is not None:
        live = runtime.sessions.get(body.session)
        if live is None:
            raise HTTPException(404, f"no runtime session {body.session!r}")
    await bridge.attach(live)
    return _out(bridge)

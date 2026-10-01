"""The API application and its published OpenAPI description."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from gws_api import ignition, opcua, runtime, world_model
from gws_opcua.server import PointServer
from gws_world_model.store import SqliteStore

TITLE = "Graphene World Sim API"
VERSION = "0.4.0"


def create_app(
    store: SqliteStore | None = None,
    opcua_endpoint: str | None = None,
    start: str | None = None,
) -> FastAPI:
    """The API application. With `opcua_endpoint`, it also serves OPC UA there while it runs.
    With `start`, it starts a session from that preset, serves it and runs it at real time."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        bridge = None
        if opcua_endpoint is not None:
            bridge = opcua.Bridge(PointServer(opcua_endpoint), app.state.store)
            await bridge.start()
            app.state.opcua = bridge
        task = asyncio.create_task(runtime.autostart(app, start)) if start else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            if bridge is not None:
                del app.state.opcua
                await bridge.stop()

    app = FastAPI(title=TITLE, version=VERSION, lifespan=lifespan)
    app.state.store = store if store is not None else SqliteStore()
    app.include_router(world_model.router, prefix="/api")
    app.include_router(runtime.router, prefix="/api")
    app.include_router(opcua.router, prefix="/api")
    app.include_router(ignition.router, prefix="/api")
    return app


def openapi_json() -> str:
    """The OpenAPI document as published in `docs/api/openapi.json`."""
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(openapi_json(), encoding="utf-8")

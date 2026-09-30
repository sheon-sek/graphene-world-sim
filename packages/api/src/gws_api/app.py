"""The API application and its published OpenAPI description."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from fastapi import FastAPI

from gws_api import runtime, world_model
from gws_world_model.store import SqliteStore

TITLE = "Graphene World Sim API"
VERSION = "0.3.0"


def create_app(store: SqliteStore | None = None) -> FastAPI:
    app = FastAPI(title=TITLE, version=VERSION)
    app.state.store = store if store is not None else SqliteStore()
    app.include_router(world_model.router, prefix="/api")
    app.include_router(runtime.router, prefix="/api")
    return app


def openapi_json() -> str:
    """The OpenAPI document as published in `docs/api/openapi.json`."""
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(openapi_json(), encoding="utf-8")

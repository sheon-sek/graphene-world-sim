"""Serve the API and the OPC UA server from one process.

    uv run python -m gws_api.serve --db world.sqlite
    # http://127.0.0.1:8000/api and opc.tcp://0.0.0.0:4840/graphene/twin

`PUT /api/opcua/session {"session": "<id>"}` chooses which runtime session OPC UA serves.
With `--web apps/web/dist`, the built web app is served at `/` beside the API.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from gws_api.app import create_app
from gws_opcua.server import ENDPOINT
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.store import SqliteStore


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gws-serve", description=__doc__.split("\n")[0])
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port")
    parser.add_argument("--opc-endpoint", default=ENDPOINT, help="OPC UA endpoint URL")
    parser.add_argument("--no-opcua", action="store_true", help="do not serve OPC UA")
    parser.add_argument("--web", type=Path, metavar="DIR", help="serve this web app build at /")
    parser.add_argument("--db", type=Path, default=Path("world.sqlite"), help="World Model store")
    parser.add_argument(
        "--import-graphene",
        type=Path,
        metavar="DIR",
        help="import the Graphene data directory as the first revision if the store is empty",
    )
    args = parser.parse_args(argv)
    store = SqliteStore(args.db)
    if args.import_graphene is not None and store.head() is None:
        store.create_revision(build(Sources.read(args.import_graphene)), "import", "gws-serve")
    app = create_app(store, opcua_endpoint=None if args.no_opcua else args.opc_endpoint)
    if args.web is not None:
        app.mount("/", StaticFiles(directory=args.web, html=True), name="web")
    # Open frame streams would otherwise hold a shutdown forever.
    uvicorn.run(app, host=args.host, port=args.port, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()

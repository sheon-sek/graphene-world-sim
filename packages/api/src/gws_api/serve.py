"""Serve the API and the OPC UA server from one process.

    uv run python -m gws_api.serve --db world.sqlite
    # http://127.0.0.1:8000/api and opc.tcp://0.0.0.0:4840/graphene/twin

`PUT /api/opcua/session {"session": "<id>"}` chooses which runtime session OPC UA serves.
With `--web apps/web/dist`, the built web app is served at `/` beside the API.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn
from fastapi.staticfiles import StaticFiles

from gws_api.app import create_app
from gws_opcua.server import ENDPOINT
from gws_world_model.importers.graphene import Sources, build
from gws_world_model.store import SqliteStore

IMPORTER = "gws-serve"


def seed(store: SqliteStore, data: Path, *, reimport: bool = False) -> str:
    """Import the Graphene data as the store's first revision, or as a new revision when the
    store holds an older import.

    A store kept from an earlier checkout holds the World Model that checkout's importer built.
    When the data or the importer has changed since, that model can disagree with the runtime
    (a controller wired the old way, points the importer now adds), so the current import is
    added on top. Revisions someone edited are left as the head unless `reimport` is given."""
    doc = build(Sources.read(data))
    head = store.head()
    if head is None:
        store.create_revision(doc, "import", IMPORTER)
        return f"Imported {data} as revision 1."
    info = store.info(head)
    if info.content_hash == doc.content_hash():
        return f"Revision {head} is the current import of {data}."
    if reimport or info.author == IMPORTER:
        added = store.create_revision(
            doc, f"re-import {data}: the data or the importer changed", IMPORTER
        )
        return f"Revision {head} was an older import; {data} is now revision {added.number}."
    return (
        f"Warning: revision {head} ({info.author}: {info.message}) differs from the current import "
        f"of {data}. Restart with --reimport to add the current import on top, or delete the "
        "store (--db) to start again from the import."
    )


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
    parser.add_argument(
        "--start",
        default="site",
        metavar="PRESET",
        help="start this preset's session, serve it over OPC UA and run it at real time: "
        "site (default), incidents, dh01-slice, or none",
    )
    parser.add_argument(
        "--reimport",
        action="store_true",
        help="with --import-graphene: add the import as a new revision even over edited revisions",
    )
    args = parser.parse_args(argv)
    store = SqliteStore(args.db)
    if args.import_graphene is not None:
        print(seed(store, args.import_graphene, reimport=args.reimport), file=sys.stderr)
    app = create_app(
        store,
        opcua_endpoint=None if args.no_opcua else args.opc_endpoint,
        start=None if args.start == "none" else args.start,
    )
    if args.web is not None:
        app.mount("/", StaticFiles(directory=args.web, html=True), name="web")
    # Open frame streams would otherwise hold a shutdown forever.
    uvicorn.run(app, host=args.host, port=args.port, timeout_graceful_shutdown=5)


if __name__ == "__main__":
    main()

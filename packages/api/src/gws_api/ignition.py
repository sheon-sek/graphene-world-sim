"""Ignition tags and UDTs generated from the World Model's point bindings (#18).

The output is an Ignition tag import document (`{"tags": [...]}`) for a tag provider's root.
Every point binding becomes one OPC tag at its export path, bound to the simulator's OPC UA
server through an OPC connection:

- A UDT definition under `_types_` is generated for each Ignition UDT type whose instances
  in the World Model all carry the same points. Its members read `nsu=<namespace>;s=point:
  {PointPath}/<member>` from the connection in its `OpcServer` parameter, and each instance
  sets `PointPath` to its own encoded export path.
- Every other point (points outside a UDT instance, and instances of types whose instances
  differ) is a plain OPC tag in folders mirroring its export path.
- Item paths name the namespace by URI, so they hold whatever index the server gives it.
- DataSet and Document points travel as JSON text, so their tags are Strings.
- A Boolean fault or alarm point gets an alarm that is active while the point is true.
- With a history provider, every tag records its history there, on change, sampled at most
  once a second.

    uv run python -m gws_api.ignition --db world.sqlite --connection "Graphene Demo Twin" \\
        > demotwin-tags.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from gws_opcua.nodeid import NAMESPACE_URI, encode_point_path, point_node_id
from gws_world_model.model import AssetSignal, PointBinding, PointClass, WorldModel

CONNECTION = "Graphene Demo Twin"
"""The gateway's existing OPC UA connection to the simulator."""
TYPES_FOLDER = "_types_"
PATH_PARAMETER = "PointPath"
SERVER_PARAMETER = "OpcServer"
TAG_TYPES = {"DataSet": "String", "Document": "String"}
FAULT_ALARM = {"name": "Active", "mode": "Equality", "setpointA": 1.0, "priority": "High"}

Tag = dict[str, Any]


def item_path(export_path: str) -> str:
    return f"nsu={NAMESPACE_URI};s={point_node_id(export_path)}"


@dataclass(frozen=True, slots=True)
class _Instance:
    asset: str
    type_id: str
    members: dict[str, PointBinding]
    """Member path relative to the instance -> binding."""


def _instances(doc: WorldModel) -> tuple[list[_Instance], list[PointBinding]]:
    """UDT instances whose type is uniform across the World Model, and the loose points."""
    grouped: dict[str, dict[str, PointBinding]] = defaultdict(dict)
    loose: list[PointBinding] = []
    for b in doc.point_bindings.values():
        src = b.source
        if (
            isinstance(src, AssetSignal)
            and b.ignition_type_id
            and b.path.startswith(src.asset + "/")
            and src.asset in doc.assets
            and doc.component_types[doc.assets[src.asset].type].ignition_type_id
            == b.ignition_type_id
        ):
            grouped[src.asset][b.path[len(src.asset) + 1 :]] = b
        else:
            loose.append(b)
    # An instance cannot hold tags its type does not define, nor sit inside another one.
    roots = set(grouped)
    blocked = {root for b in loose for root in _prefixes(b.path) if root in roots}
    blocked |= {root for root in roots if any(p in roots for p in _prefixes(root))}
    shapes: dict[str, set[frozenset[tuple[str, str, str | None]]]] = defaultdict(set)
    for members in grouped.values():
        type_id = next(iter(members.values())).ignition_type_id
        assert type_id is not None
        shapes[type_id].add(frozenset((m, b.data_type, b.unit) for m, b in members.items()))
    instances: list[_Instance] = []
    for asset, members in sorted(grouped.items()):
        type_id = next(iter(members.values())).ignition_type_id
        assert type_id is not None
        if asset in blocked or len(shapes[type_id]) != 1:
            loose.extend(members.values())
        else:
            instances.append(_Instance(asset, type_id, members))
    return instances, loose


def _prefixes(path: str) -> Iterable[str]:
    parts = path.split("/")
    return ("/".join(parts[:n]) for n in range(1, len(parts)))


class _Tree:
    """Folders of tags keyed by path segment."""

    def __init__(self) -> None:
        self.root: Tag = {"tags": {}}

    def put(self, path: str, tag: Tag) -> None:
        *folders, name = path.split("/")
        node = self.root
        for folder in folders:
            child = node["tags"].setdefault(
                folder, {"name": folder, "tagType": "Folder", "tags": {}}
            )
            if child["tagType"] != "Folder":
                raise ValueError(f"{path}: {folder!r} is a tag, not a folder")
            node = child
        if name in node["tags"]:
            raise ValueError(f"duplicate tag {path!r}")
        node["tags"][name] = {"name": name, **tag}

    def tags(self) -> list[Tag]:
        def render(node: Tag) -> list[Tag]:
            out = []
            for name in sorted(node["tags"]):
                child = dict(node["tags"][name])
                if isinstance(child.get("tags"), dict):
                    child["tags"] = render(child)
                out.append(child)
            return out

        return render(self.root)


def _atomic(
    b: PointBinding, server: Any, path: Any, alarms: bool, history: str | None = None
) -> Tag:
    tag: Tag = {
        "tagType": "AtomicTag",
        "dataType": TAG_TYPES.get(b.data_type, b.data_type),
        "valueSource": "opc",
        "opcServer": server,
        "opcItemPath": path,
    }
    if b.unit:
        tag["engUnit"] = b.unit
    if alarms and b.point_class is PointClass.FAULT_ALARM and b.data_type == "Boolean":
        tag["alarms"] = [dict(FAULT_ALARM)]
    if history:
        tag |= {
            "historyEnabled": True,
            "historyProvider": history,
            "historySampleRate": 1000,
            "historySampleRateUnits": "MS",
            "historyMode": "OnChange",
        }
    return tag


def generate(
    doc: WorldModel,
    connection: str = CONNECTION,
    *,
    alarms: bool = True,
    history: str | None = None,
) -> Tag:
    """The tag import document for a provider root. `history` names a historian provider for
    the tags to record their history to."""
    instances, loose = _instances(doc)
    types = _Tree()
    defined: set[str] = set()
    tree = _Tree()
    for inst in instances:
        if inst.type_id not in defined:
            members = _Tree()
            for member, b in sorted(inst.members.items()):
                binding = {
                    "bindType": "parameter",
                    "binding": f"nsu={NAMESPACE_URI};s=point:{{{PATH_PARAMETER}}}/"
                    + encode_point_path(member),
                }
                server = {"bindType": "parameter", "binding": f"{{{SERVER_PARAMETER}}}"}
                members.put(member, _atomic(b, server, binding, alarms, history))
            types.put(
                inst.type_id,
                {
                    "tagType": "UdtType",
                    "parameters": {
                        PATH_PARAMETER: {"dataType": "String", "value": ""},
                        SERVER_PARAMETER: {"dataType": "String", "value": connection},
                    },
                    "tags": members.root["tags"],
                },
            )
            defined.add(inst.type_id)
        tree.put(
            inst.asset,
            {
                "tagType": "UdtInstance",
                "typeId": inst.type_id,
                "parameters": {
                    PATH_PARAMETER: {"dataType": "String", "value": encode_point_path(inst.asset)},
                    SERVER_PARAMETER: {"dataType": "String", "value": connection},
                },
            },
        )
    for b in sorted(loose, key=lambda b: b.path):
        tree.put(b.path, _atomic(b, connection, item_path(b.path), alarms, history))
    tags = tree.tags()
    if defined:
        tags.insert(0, {"name": TYPES_FOLDER, "tagType": "Folder", "tags": types.tags()})
    return {"tags": tags}


def resolve(document: Tag) -> dict[str, tuple[str, str, str]]:
    """Every OPC tag a gateway would run from the document: tag path -> (OPC connection, item
    path, data type), with UDT parameters substituted. Used to check the output."""
    types: dict[str, Tag] = {}

    def collect(nodes: list[Tag], prefix: str) -> None:
        for node in nodes:
            path = f"{prefix}/{node['name']}" if prefix else node["name"]
            if node["tagType"] == "UdtType":
                types[path] = node
            else:
                collect(node.get("tags", []), path)

    out: dict[str, tuple[str, str, str]] = {}

    def expand(nodes: list[Tag], prefix: str, params: dict[str, str]) -> None:
        for node in nodes:
            path = f"{prefix}/{node['name']}" if prefix else node["name"]
            kind = node["tagType"]
            if kind == "Folder":
                expand(node.get("tags", []), path, params)
            elif kind == "UdtInstance":
                udt = types[node["typeId"]]
                values = {k: v["value"] for k, v in udt["parameters"].items()}
                values |= {k: v["value"] for k, v in node.get("parameters", {}).items()}
                expand(udt["tags"], path, values)
            elif kind == "AtomicTag":
                server, item = node["opcServer"], node["opcItemPath"]
                if isinstance(server, dict):
                    server = _substitute(server["binding"], params)
                if isinstance(item, dict):
                    item = _substitute(item["binding"], params)
                out[path] = (server, item, node["dataType"])

    top = document["tags"]
    for node in top:
        if node["name"] == TYPES_FOLDER:
            collect(node["tags"], "")
    expand([n for n in top if n["name"] != TYPES_FOLDER], "", {})
    return out


def _substitute(template: str, params: dict[str, str]) -> str:
    for key, value in params.items():
        template = template.replace("{" + key + "}", value)
    return template


router = APIRouter(prefix="/ignition", tags=["ignition"])


@router.get("/tags")
def tags(
    request: Request,
    revision: Annotated[int | None, Query(ge=1, description="Default: the head.")] = None,
    connection: Annotated[str, Query(min_length=1)] = CONNECTION,
    alarms: bool = True,
    history: Annotated[str | None, Query(description="Historian provider to record to")] = None,
) -> dict[str, Any]:
    """The tag import document for a tag provider bound to the simulator."""
    from gws_world_model.store import NotFound, SqliteStore

    store: SqliteStore = request.app.state.store
    number = revision or store.head()
    if number is None:
        raise HTTPException(409, "the World Model has no revision yet")
    try:
        doc = store.get(number)
    except NotFound as e:
        raise HTTPException(404, str(e)) from e
    return generate(doc, connection, alarms=alarms, history=history)


def main(argv: list[str] | None = None) -> None:
    from gws_world_model.importers.graphene import Sources, build
    from gws_world_model.store import SqliteStore

    parser = argparse.ArgumentParser(prog="gws-ignition-tags", description=__doc__.split("\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--db", type=Path, help="World Model store (its head revision)")
    source.add_argument("--graphene", type=Path, metavar="DIR", help="Graphene data directory")
    parser.add_argument("--revision", type=int, help="revision of --db (default: the head)")
    parser.add_argument("--connection", default=CONNECTION, help="OPC connection name")
    parser.add_argument("--no-alarms", action="store_true", help="omit fault alarms")
    parser.add_argument("--history", metavar="PROVIDER", help="record history to this provider")
    args = parser.parse_args(argv)
    if args.graphene is not None:
        doc = build(Sources.read(args.graphene))
    else:
        store = SqliteStore(args.db)
        revision = args.revision or store.head()
        if revision is None:
            parser.error(f"{args.db} has no revision")
        doc = store.get(revision)
    json.dump(
        generate(doc, args.connection, alarms=not args.no_alarms, history=args.history),
        sys.stdout,
        indent=1,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()

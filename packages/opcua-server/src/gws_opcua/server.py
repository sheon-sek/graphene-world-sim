"""The OPC UA server in compatibility mode (ADR-0003).

It publishes the points it is given, and nothing else:

- Namespace `urn:eetarp:graphene:demo:twin`, registered first so its index is 2, as the
  gateway's existing item paths (`ns=2;s=point:…`) expect.
- Each point is a variable `point:<encodedExportPath>` under folders `folder:<exportPath>` that
  mirror its path from `Objects`. BrowseNames are the path segments, unchanged.
- The variable's DataType is the OPC UA built-in type of its Ignition data type; DataSet and
  Document are JSON text in a String.
- The StatusCode follows the value's quality, with the specific code its reason names
  (`comm_lost` → BadCommunicationError, `sensor_failed` → BadSensorFailure, `out_of_range` →
  UncertainEngineeringUnitsExceeded, …). SourceTimestamp is the timestamp of the value's last
  change: points are published by exception.
- A command point is readable and writable. A write is converted to the point's data type and
  handed to the WriteHandler; the server answers Good only when the handler accepts it.
  Every other point answers BadNotWritable, whoever the client is.
- `set_points` adds and removes nodes in place and fires a GeneralModelChangeEvent from the
  Server object, so connected clients see new points without reconnecting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from asyncua import Server, ua
from asyncua.crypto.permission_rules import User, UserRole

from gws_opcua.nodeid import NAMESPACE_URI, folder_node_id, point_node_id
from gws_opcua.points import (
    DATA_TYPES,
    Mismatch,
    PointSpec,
    PointValue,
    Scalar,
    WriteHandler,
    coerce,
    default,
)

ENDPOINT = "opc.tcp://0.0.0.0:4840/graphene/twin"
"""The endpoint the gateway's `Graphene Demo Twin` connection points at."""
SERVER_NAME = "Graphene Demo Twin"
NAMESPACE_INDEX = 2

VARIANT_TYPES: dict[str, ua.VariantType] = {
    "Float4": ua.VariantType.Float,
    "Float8": ua.VariantType.Double,
    "Int4": ua.VariantType.Int32,
    "Int8": ua.VariantType.Int64,
    "Boolean": ua.VariantType.Boolean,
    "String": ua.VariantType.String,
    "DateTime": ua.VariantType.DateTime,
    "DataSet": ua.VariantType.String,
    "Document": ua.VariantType.String,
}

_QUALITY = {
    "good": ua.StatusCodes.Good,
    "uncertain": ua.StatusCodes.Uncertain,
    "bad": ua.StatusCodes.Bad,
}
_REASON = {
    "comm_lost": ua.StatusCodes.BadCommunicationError,
    "sensor_failed": ua.StatusCodes.BadSensorFailure,
    "out_of_range": ua.StatusCodes.UncertainEngineeringUnitsExceeded,
    "unbound": ua.StatusCodes.BadConfigurationError,
    "not_simulated": ua.StatusCodes.BadConfigurationError,
    "out_of_scope": ua.StatusCodes.BadOutOfService,
}
MODEL_CHANGE_DETAIL = 1000
"""Up to this many changes, the model-change event lists each node; above it, one change on
the Objects folder tells clients to browse again."""

_LOCAL = User(role=UserRole.Admin)
"""The user asyncua assumes for a write made inside the process."""

_log = logging.getLogger(__name__)


def status_code(quality: str, reason: str = "") -> ua.StatusCode:
    """The StatusCode of a value with this quality and reason."""
    base = _QUALITY.get(quality)
    if base is None:
        raise ValueError(f"unknown quality {quality!r}")
    specific = _REASON.get(reason)
    if specific is not None and ua.StatusCode(specific).is_bad() == (quality == "bad"):
        if quality != "uncertain" or ua.StatusCode(specific).is_uncertain():
            return ua.StatusCode(specific)
    return ua.StatusCode(base)


def folders_of(paths: Iterable[str]) -> set[str]:
    """Every folder path the points need, parents included."""
    folders: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for n in range(1, len(parts)):
            folders.add("/".join(parts[:n]))
    return folders


@dataclass(frozen=True, slots=True)
class ModelChange:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    """Point paths."""


class PointServer:
    """An asyncua server publishing points by path."""

    def __init__(
        self,
        endpoint: str = ENDPOINT,
        *,
        name: str = SERVER_NAME,
        on_write: WriteHandler | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`clock` gives the SourceTimestamp of an accepted write's echo (default: now)."""
        self.endpoint = endpoint
        self.name = name
        self.on_write = on_write
        self.clock = clock or (lambda: datetime.now(UTC))
        self.specs: dict[str, PointSpec] = {}
        self._server: Server | None = None
        self._folders: set[str] = set()
        self._by_node: dict[ua.NodeId, str] = {}
        self._last: dict[str, tuple[Any, str, str]] = {}

    # --- lifecycle -------------------------------------------------------------------------

    async def start(self) -> None:
        logging.getLogger("asyncua").setLevel(logging.ERROR)
        server = Server()
        await server.init()
        server.set_endpoint(self.endpoint)
        server.set_server_name(self.name)
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        index = await server.register_namespace(NAMESPACE_URI)
        if index != NAMESPACE_INDEX:
            raise RuntimeError(f"namespace index {index}, expected {NAMESPACE_INDEX}")
        service = server.iserver.attribute_service
        passthrough = service.write

        async def write(params: ua.WriteParameters, user: User = _LOCAL) -> list[ua.StatusCode]:
            return await self._write(params, user, passthrough)

        service.write = write
        await server.start()
        self._server = server
        specs = list(self.specs.values())
        self.specs = {}
        await self._apply(specs, announce=False)

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def __aenter__(self) -> PointServer:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.stop()

    @property
    def server(self) -> Server:
        if self._server is None:
            raise RuntimeError("the OPC UA server is not running")
        return self._server

    def node_id(self, path: str) -> ua.NodeId:
        return ua.NodeId(point_node_id(path), NAMESPACE_INDEX)

    # --- address space ---------------------------------------------------------------------

    async def set_points(self, specs: Iterable[PointSpec]) -> ModelChange:
        """Make the address space hold exactly these points. Points whose spec changed are
        replaced. Connected clients get a model-change event."""
        specs = list(specs)
        if self._server is None:
            before = set(self.specs)
            self.specs = {s.path: s for s in specs}
            return ModelChange(tuple(sorted(set(self.specs) - before)), ())
        return await self._apply(specs, announce=True)

    async def _apply(self, specs: list[PointSpec], *, announce: bool) -> ModelChange:
        wanted: dict[str, PointSpec] = {}
        for spec in specs:
            if spec.data_type not in DATA_TYPES:
                raise ValueError(f"{spec.path}: unknown data type {spec.data_type!r}")
            wanted[spec.path] = spec
        folders = folders_of(wanted)
        clash = sorted(folders & set(wanted))
        if clash:
            raise ValueError(f"paths that are both a point and a folder: {clash[:5]}")
        removed = sorted(p for p, s in self.specs.items() if wanted.get(p) != s)
        added = sorted(p for p, s in wanted.items() if self.specs.get(p) != s)
        stale_folders = sorted(self._folders - folders, key=lambda f: -f.count("/"))
        new_folders = sorted(folders - self._folders, key=lambda f: f.count("/"))
        isession = self.server.iserver.isession

        doomed = [self.node_id(p) for p in removed] + [self._folder_id(f) for f in stale_folders]
        if doomed:
            await isession.delete_nodes(
                ua.DeleteNodesParameters(
                    NodesToDelete=[
                        ua.DeleteNodesItem(NodeId=n, DeleteTargetReferences=True) for n in doomed
                    ]
                )
            )
        for path in removed:
            del self.specs[path]
            self._by_node.pop(self.node_id(path), None)
            self._last.pop(path, None)
        self._folders -= set(stale_folders)

        items = [self._folder_item(f) for f in new_folders]
        items += [self._variable_item(wanted[p]) for p in added]
        if items:
            results = await isession.add_nodes(items)
            failed = [
                (item.RequestedNewNodeId.to_string(), r.StatusCode)
                for item, r in zip(items, results, strict=True)
                if not r.StatusCode.is_good()
            ]
            if failed:
                raise RuntimeError(f"could not add nodes: {failed[:5]}")
        self._folders |= set(new_folders)
        for path in added:
            self.specs[path] = wanted[path]
            self._by_node[self.node_id(path)] = path

        change = ModelChange(tuple(added), tuple(removed))
        if announce and (added or removed or stale_folders or new_folders):
            await self._announce(change, new_folders, stale_folders)
        return change

    def _folder_id(self, path: str) -> ua.NodeId:
        return ua.NodeId(folder_node_id(path), NAMESPACE_INDEX)

    def _parent(self, path: str) -> ua.NodeId:
        head, _, _ = path.rpartition("/")
        return self._folder_id(head) if head else ua.NodeId(ua.ObjectIds.ObjectsFolder)

    def _folder_item(self, path: str) -> ua.AddNodesItem:
        name = path.rsplit("/", 1)[-1]
        attrs = ua.ObjectAttributes()
        attrs.DisplayName = ua.LocalizedText(name)
        attrs.EventNotifier = 0
        attrs.SpecifiedAttributes = ua.NodeAttributesMask.DisplayName
        item = ua.AddNodesItem()
        item.RequestedNewNodeId = self._folder_id(path)
        item.BrowseName = ua.QualifiedName(name, NAMESPACE_INDEX)
        item.NodeClass = ua.NodeClass.Object
        item.ParentNodeId = self._parent(path)
        item.ReferenceTypeId = ua.NodeId(ua.ObjectIds.Organizes)
        item.TypeDefinition = ua.NodeId(ua.ObjectIds.FolderType)
        item.NodeAttributes = attrs
        return item

    def _variable_item(self, spec: PointSpec) -> ua.AddNodesItem:
        name = spec.path.rsplit("/", 1)[-1]
        vtype = VARIANT_TYPES[spec.data_type]
        access = ua.AccessLevel.CurrentRead.mask
        if spec.writable:
            access |= ua.AccessLevel.CurrentWrite.mask
        attrs = ua.VariableAttributes()
        attrs.DisplayName = ua.LocalizedText(name)
        attrs.Value = ua.Variant(default(spec.data_type), vtype)
        attrs.DataType = ua.NodeId(vtype.value)
        attrs.ValueRank = ua.ValueRank.Scalar
        attrs.AccessLevel = access
        attrs.UserAccessLevel = access
        attrs.SpecifiedAttributes = (
            ua.NodeAttributesMask.DisplayName
            | ua.NodeAttributesMask.Value
            | ua.NodeAttributesMask.DataType
            | ua.NodeAttributesMask.ValueRank
            | ua.NodeAttributesMask.AccessLevel
            | ua.NodeAttributesMask.UserAccessLevel
        )
        item = ua.AddNodesItem()
        item.RequestedNewNodeId = self.node_id(spec.path)
        item.BrowseName = ua.QualifiedName(name, NAMESPACE_INDEX)
        item.NodeClass = ua.NodeClass.Variable
        item.ParentNodeId = self._parent(spec.path)
        item.ReferenceTypeId = ua.NodeId(ua.ObjectIds.HasComponent)
        item.TypeDefinition = ua.NodeId(ua.ObjectIds.BaseDataVariableType)
        item.NodeAttributes = attrs
        return item

    async def _announce(
        self, change: ModelChange, new_folders: list[str], stale_folders: list[str]
    ) -> None:
        variable_type = ua.NodeId(ua.ObjectIds.BaseDataVariableType)
        folder_type = ua.NodeId(ua.ObjectIds.FolderType)
        added, deleted = (
            ua.ModelChangeStructureVerbMask.NodeAdded,
            ua.ModelChangeStructureVerbMask.NodeDeleted,
        )
        entries = [(self._folder_id(f), folder_type, added) for f in new_folders]
        entries += [(self.node_id(p), variable_type, added) for p in change.added]
        entries += [(self.node_id(p), variable_type, deleted) for p in change.removed]
        entries += [(self._folder_id(f), folder_type, deleted) for f in stale_folders]
        if len(entries) > MODEL_CHANGE_DETAIL:
            entries = [(ua.NodeId(ua.ObjectIds.ObjectsFolder), folder_type, added | deleted)]
        generator = await self.server.get_event_generator(ua.ObjectIds.GeneralModelChangeEventType)
        # asyncua types the field by its structure DataType; it travels as ExtensionObjects.
        generator.event.data_types["Changes"] = ua.VariantType.ExtensionObject
        generator.event.Changes = [
            ua.ModelChangeStructureDataType(Affected=node, AffectedType=kind, Verb=int(verb))
            for node, kind, verb in entries
        ]
        await generator.trigger(message=f"{len(change.added)} added, {len(change.removed)} removed")

    # --- values ----------------------------------------------------------------------------

    def data_value(self, spec: PointSpec, value: PointValue) -> ua.DataValue:
        vtype = VARIANT_TYPES[spec.data_type]
        status = status_code(value.quality, value.reason)
        carried: Scalar | None = None
        if value.value is not None:
            try:
                carried = coerce(value.value, spec.data_type)
            except Mismatch:
                status = ua.StatusCode(ua.StatusCodes.BadTypeMismatch)
        if carried is None and status.is_good():
            status = ua.StatusCode(ua.StatusCodes.BadWaitingForInitialData)
        if carried is None:
            carried = default(spec.data_type)
        return ua.DataValue(
            Value=ua.Variant(carried, vtype),
            StatusCode=status,
            SourceTimestamp=value.timestamp,
            ServerTimestamp=datetime.now(UTC),
        )

    async def publish(self, values: Mapping[str, PointValue]) -> int:
        """Write the points' current values by exception: unknown paths are ignored, and a
        point whose value, quality and reason are unchanged is not written again, so it keeps
        the SourceTimestamp of the step that last changed it (ADR-0003 Amendment 2). Returns
        how many were written."""
        iserver = self.server.iserver
        written = 0
        for path, value in values.items():
            spec = self.specs.get(path)
            if spec is None:
                continue
            key = (value.value, value.quality, value.reason)
            if self._last.get(path) == key:
                continue
            dv = self.data_value(spec, value)
            await iserver.write_attribute_value(self.node_id(path), dv)
            self._last[path] = key
            written += 1
        return written

    # --- writes ----------------------------------------------------------------------------

    async def _write(
        self, params: ua.WriteParameters, user: User, passthrough: Any
    ) -> list[ua.StatusCode]:
        results: list[ua.StatusCode | None] = []
        others: list[int] = []
        for i, item in enumerate(params.NodesToWrite):
            path = self._by_node.get(item.NodeId)
            if path is None:
                others.append(i)
                results.append(None)
            else:
                results.append(await self._write_point(path, item))
        if others:
            rest = ua.WriteParameters(NodesToWrite=[params.NodesToWrite[i] for i in others])
            for i, status in zip(others, await passthrough(rest, user), strict=True):
                results[i] = status
        return [
            r if r is not None else ua.StatusCode(ua.StatusCodes.BadInternalError) for r in results
        ]

    async def _write_point(self, path: str, item: ua.WriteValue) -> ua.StatusCode:
        spec = self.specs[path]
        if item.AttributeId != ua.AttributeIds.Value or not spec.writable:
            return ua.StatusCode(ua.StatusCodes.BadNotWritable)
        if self.on_write is None:
            return ua.StatusCode(ua.StatusCodes.BadNotWritable)
        raw = item.Value.Value.Value if item.Value and item.Value.Value else None
        try:
            value = coerce(raw, spec.data_type)
        except Mismatch:
            return ua.StatusCode(ua.StatusCodes.BadTypeMismatch)
        rejected = await self.on_write(path, value)
        if rejected is not None:
            _log.info("write to %s rejected: %s", path, rejected)
            return ua.StatusCode(ua.StatusCodes.BadOutOfRange)
        await self.publish({path: PointValue(value, "good", self.clock())})
        return ua.StatusCode()

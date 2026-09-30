"""The control network: which assets' data can reach a gateway (ADR-0002: networkx).

Built from World Model data only:

- **Nodes** are the assets that are ends of `net` connections, plus every asset whose type has
  a `net` port. **Links** are the `net` connections, keyed by connection id.
- **Gateways** are nodes whose `role` parameter is `gateway`. A gateway is where field data
  reaches Ignition, so a node is reachable while it is up and a path of up nodes and up links
  joins it to an up gateway.
- **Attached equipment**: an asset that is not a node reports through every gateway whose
  `serves` parameter lists its `system` (comma-separated). It is reachable while one of those
  gateways is reachable. That is how equipment with no `net` port of its own reports.
- **Views**: an asset whose `device` parameter names a node (a port-level view of a switch)
  shares that node's state. Failing the view fails the node.
- Any other asset has no reporting path in the data and is reachable by default.

A failure is an element: a node or view asset id, a `net` connection id, or a switch port
(`<asset>/Ports/Port NN`, for assets with a `port_count`). A failed port reports its link down;
the data does not say which connection a port carries, so it does not change reachability.
Nothing here names an asset: losing a switch makes everything behind it unreachable because
there is no other path, not because of a rule.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from typing import Any

import networkx as nx

from gws_world_model.model import Asset, Domain, Scalar, WorldModel

GATEWAY = "gateway"
MONITOR_SIGNALS = frozenset({"Comm", "Status"})
"""Signals the gateway's monitoring computes about a device (ping, SNMP poll status). They
stay fresh when the device itself cannot report; every other signal of a network asset is
reported by the device and goes stale with it."""

# Ignition state maps of the Network Device, Network Switch and Network Switch Port UDTs.
COMM_CONNECTED, COMM_DISCONNECTED = 0, 1
STATUS_HEALTHY, STATUS_OFFLINE = 0, 1
LINK_UP, LINK_DOWN = 1, 2
ADMIN_UP = 1
DISPLAY_OFF, DISPLAY_UP, DISPLAY_DOWN = 0, 1, 2


def _parameter(doc: WorldModel, asset: Asset, name: str) -> Scalar | None:
    if name in asset.parameters:
        return asset.parameters[name]
    spec = doc.component_types[asset.type].parameters.get(name)
    return None if spec is None else spec.default


def _port_name(n: int) -> str:
    return f"Port {n:02d}"


class ControlNetwork:
    def __init__(
        self,
        graph: nx.MultiGraph,
        gateways: Iterable[str],
        attached: Mapping[str, tuple[str, ...]],
        views: Mapping[str, str],
        port_counts: Mapping[str, int],
    ) -> None:
        self.graph = graph
        self.gateways = tuple(sorted(gateways))
        self.attached = dict(attached)
        self.views = dict(views)
        self.port_counts = dict(port_counts)
        self._links = {key: (u, v) for u, v, key in graph.edges(keys=True)}
        self._failed_nodes: set[str] = set()
        self._failed_links: set[str] = set()
        self._failed_ports: set[str] = set()
        self._paths: dict[str, tuple[str, ...]] = {}
        self._signals: dict[str, dict[str, float | bool]] = {}
        self._dirty = True
        self._recompute()

    @classmethod
    def from_world(cls, doc: WorldModel) -> ControlNetwork:
        graph: nx.MultiGraph = nx.MultiGraph()
        net_ended = {
            end.node
            for c in doc.connections.values()
            if c.domain is Domain.NET
            for end in (c.source, c.target)
            if not end.is_room
        }
        views: dict[str, str] = {}
        port_counts: dict[str, int] = {}
        for asset in sorted(doc.assets.values(), key=lambda a: a.id):
            ports = doc.component_types[asset.type].ports.values()
            device = _parameter(doc, asset, "device")
            if isinstance(device, str) and device in doc.assets and asset.id not in net_ended:
                views[asset.id] = device
            elif asset.id in net_ended or any(p.domain is Domain.NET for p in ports):
                graph.add_node(asset.id)
            count = _parameter(doc, asset, "port_count")
            if isinstance(count, int | float) and not isinstance(count, bool):
                port_counts[asset.id] = int(count)
        for cid in sorted(doc.connections):
            c = doc.connections[cid]
            if c.domain is Domain.NET and not (c.source.is_room or c.target.is_room):
                u = views.get(c.source.node, c.source.node)
                v = views.get(c.target.node, c.target.node)
                if u != v:
                    graph.add_edge(u, v, key=cid)
        gateways = [n for n in graph.nodes if _parameter(doc, doc.assets[n], "role") == GATEWAY]
        serves: dict[str, list[str]] = {}
        for g in sorted(gateways):
            listed = _parameter(doc, doc.assets[g], "serves")
            for system in str(listed or "").split(","):
                if system.strip():
                    serves.setdefault(system.strip(), []).append(g)
        attached = {
            a.id: tuple(serves[a.system])
            for a in doc.assets.values()
            if a.id not in graph and a.id not in views and a.system in serves
        }
        return cls(graph, gateways, attached, views, port_counts)

    # --- failures --------------------------------------------------------------------------

    def _element(self, element: str) -> tuple[set[str], str]:
        element = self.views.get(element, element)
        if element in self.graph:
            return self._failed_nodes, element
        if element in self._links:
            return self._failed_links, element
        asset, sep, port = element.partition("/Ports/")
        node = self.views.get(asset, asset)
        count = self.port_counts.get(asset, self.port_counts.get(node))
        if sep and count is not None and port in {_port_name(n) for n in range(1, count + 1)}:
            return self._failed_ports, f"{node}/Ports/{port}"
        raise KeyError(f"{element!r} is not a network node, net connection or switch port")

    def fail(self, element: str) -> None:
        failed, key = self._element(element)
        if key not in failed:
            failed.add(key)
            self._dirty = True

    def restore(self, element: str) -> None:
        failed, key = self._element(element)
        if key in failed:
            failed.discard(key)
            self._dirty = True

    def failed(self) -> tuple[str, ...]:
        return tuple(sorted(self._failed_nodes | self._failed_links | self._failed_ports))

    # --- stepping --------------------------------------------------------------------------

    def step(self, t: float) -> None:
        """Recompute reachability, only when a failure or restore happened since the last step."""
        del t
        if self._dirty:
            self._recompute()

    def _link_up(self, u: str, v: str) -> bool:
        return any(key not in self._failed_links for key in self.graph[u][v])

    def _recompute(self) -> None:
        """Breadth-first search from every up gateway over up nodes and links. Neighbours are
        visited in sorted order, so paths are deterministic."""
        parent: dict[str, str | None] = {}
        queue: deque[str] = deque()
        for g in self.gateways:
            if g not in self._failed_nodes:
                parent[g] = None
                queue.append(g)
        while queue:
            u = queue.popleft()
            for v in sorted(self.graph.adj[u]):
                if v in parent or v in self._failed_nodes or not self._link_up(u, v):
                    continue
                parent[v] = u
                queue.append(v)
        paths: dict[str, tuple[str, ...]] = {}
        for node in parent:
            hops: list[str] = []
            step = parent[node]
            while step is not None:
                hops.append(step)
                step = parent[step]
            paths[node] = tuple(hops)
        self._paths = paths
        self._dirty = False
        self._signals = self._build_signals()

    # --- queries ---------------------------------------------------------------------------

    def is_network_asset(self, asset: str) -> bool:
        return asset in self.graph or asset in self.views

    def up(self, asset: str) -> bool:
        return self.views.get(asset, asset) not in self._failed_nodes

    def reachable(self, asset: str) -> bool:
        """Can this asset's data reach a gateway? True for an asset with no reporting path in
        the data (it is not on the modelled network)."""
        node = self.views.get(asset, asset)
        if node in self.graph:
            return node in self._paths
        gateways = self.attached.get(asset)
        if gateways is not None:
            return any(g in self._paths for g in gateways)
        return True

    def path(self, asset: str) -> tuple[str, ...]:
        """The nodes the asset currently reports through, from the next hop to the gateway;
        empty for a gateway, an unreachable asset or one not on the network."""
        node = self.views.get(asset, asset)
        if node in self.graph:
            return self._paths.get(node, ())
        for g in self.attached.get(asset, ()):
            if g in self._paths:
                return (g, *self._paths[g])
        return ()

    def signals(self) -> dict[str, dict[str, float | bool]]:
        """Per network asset (nodes and views): `up`, `reachable`, `links_up`, `links`, the
        UDTs' `Comm` and `Status`, and for switches the per-port link state and port counts."""
        return {asset: dict(values) for asset, values in self._signals.items()}

    def signal(self, asset: str, name: str) -> float | bool | None:
        return self._signals.get(asset, {}).get(name)

    def _build_signals(self) -> dict[str, dict[str, float | bool]]:
        out: dict[str, dict[str, float | bool]] = {}
        for asset in sorted([*self.graph.nodes, *self.views]):
            node = self.views.get(asset, asset)
            up, reachable = node not in self._failed_nodes, node in self._paths
            neighbours = list(self.graph.adj[node])
            values: dict[str, float | bool] = {
                "up": up,
                "reachable": reachable,
                "links": sum(len(self.graph[node][n]) for n in neighbours),
                "links_up": sum(
                    1
                    for n in neighbours
                    for key in self.graph[node][n]
                    if up and key not in self._failed_links and n not in self._failed_nodes
                ),
                "Comm": COMM_CONNECTED if reachable else COMM_DISCONNECTED,
                "Status": STATUS_HEALTHY if up and reachable else STATUS_OFFLINE,
            }
            count = self.port_counts.get(asset, self.port_counts.get(node))
            if count is not None:
                values.update(self._port_signals(node, count, up))
            out[asset] = values
        return out

    def _port_signals(self, node: str, count: int, up: bool) -> dict[str, float | bool]:
        values: dict[str, float | bool] = {"Port Count": count}
        n_up = n_down = 0
        for n in range(1, count + 1):
            name = _port_name(n)
            link_up = up and f"{node}/Ports/{name}" not in self._failed_ports
            n_up += link_up
            n_down += not link_up
            values[f"Ports/{name}/Admin Status"] = ADMIN_UP
            values[f"Ports/{name}/Link Status"] = LINK_UP if link_up else LINK_DOWN
            values[f"Ports/{name}/Display Status"] = DISPLAY_UP if link_up else DISPLAY_DOWN
        values.update({"Ports Up": n_up, "Ports Down": n_down, "Ports Off": 0})
        return values

    # --- lifecycle -------------------------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "nodes": sorted(self._failed_nodes),
            "links": sorted(self._failed_links),
            "ports": sorted(self._failed_ports),
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        self._failed_nodes = set(state.get("nodes", ()))
        self._failed_links = set(state.get("links", ()))
        self._failed_ports = set(state.get("ports", ()))
        self._recompute()

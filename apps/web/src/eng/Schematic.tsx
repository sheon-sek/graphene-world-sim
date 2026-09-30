import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  type Connection as FlowConnection,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo } from "react";
import type { Asset, Edit } from "../api/client";
import { shortName } from "../app/format";
import { useSim } from "../live/store";
import type { WorldData } from "../live/world";
import { connectionId, type Edited } from "./draft";
import { DOMAIN_COLOUR, planAt, schematic, type AssetNodeData, type RoomNodeData } from "./graph";

function AssetNode({ data, selected }: NodeProps<Node<AssetNodeData>>) {
  const ports = Object.entries(data.type?.ports ?? {});
  const ins = ports.filter(([, p]) => p.direction !== "out");
  const outs = ports.filter(([, p]) => p.direction !== "in");
  return (
    <div className={`sch-node ${data.role} ${data.edited ? "edited" : ""} ${selected ? "selected" : ""}`} data-testid={`node-${data.asset.id}`}>
      {ins.map(([name, p], i) => (
        <Handle
          key={`in:${name}`}
          id={name}
          type="target"
          position={Position.Left}
          style={{ top: `${((i + 1) / (ins.length + 1)) * 100}%`, background: DOMAIN_COLOUR[p.domain] }}
          title={`${name} (${p.domain}, ${p.direction})`}
        />
      ))}
      <span className="sch-type">{data.type?.name ?? data.asset.type}</span>
      <b>{data.asset.name}</b>
      {outs.map(([name, p], i) => (
        <Handle
          key={`out:${name}`}
          id={name}
          type="source"
          position={Position.Right}
          style={{ top: `${((i + 1) / (outs.length + 1)) * 100}%`, background: DOMAIN_COLOUR[p.domain] }}
          title={`${name} (${p.domain}, ${p.direction})`}
        />
      ))}
    </div>
  );
}

function RoomNode({ data }: NodeProps<Node<RoomNodeData>>) {
  return (
    <div className="sch-room" style={{ width: data.width, height: data.height }}>
      <Handle id="air" type="target" position={Position.Left} style={{ background: DOMAIN_COLOUR.air }} title="Room air" />
      <span>{data.name}</span>
    </div>
  );
}

function FloorNode({ data }: NodeProps<Node<{ name: string }>>) {
  return <div className="sch-floor">{data.name}</div>;
}

const NODE_TYPES = { asset: AssetNode, room: RoomNode, floor: FloorNode };

/**
 * The session's assets and connections laid out on the plan (#53). Drag an asset to place it,
 * drag from an outlet to an inlet of the same domain to connect them, and select a
 * connection and press Delete to remove it. Every change is staged in the draft.
 */
export function Schematic({
  world,
  edited,
  scope,
  added,
  stage,
}: {
  world: WorldData;
  edited: Edited;
  scope: Set<string>;
  added: Set<string>;
  stage: (edits: Edit[]) => Promise<boolean>;
}) {
  const selected = useSim((s) => s.selected);
  const select = useSim((s) => s.select);
  const graph = useMemo(() => schematic(world, edited, scope, added), [world, edited, scope, added]);
  const nodes: Node[] = useMemo(
    () =>
      graph.nodes.map((n) => ({
        id: n.id,
        type: n.kind,
        position: { x: n.x, y: n.y },
        data: n.data,
        draggable: n.kind === "asset",
        selectable: n.kind === "asset",
        deletable: false,
        selected: n.id === selected,
        zIndex: n.kind === "room" ? -1 : 1,
      })),
    [graph, selected],
  );
  const edges: Edge[] = useMemo(
    () =>
      graph.edges.map((e) => ({
        id: e.id,
        source: e.connection.source.node,
        sourceHandle: e.connection.source.port,
        target: e.connection.target.node,
        targetHandle: e.connection.target.node.startsWith("room:") ? "air" : e.connection.target.port,
        style: { stroke: DOMAIN_COLOUR[e.connection.domain] ?? "#8fa3b8", strokeWidth: e.edited ? 2.6 : 1.4, strokeDasharray: e.edited ? "6 4" : undefined },
        label: e.edited ? e.connection.domain : undefined,
      })),
    [graph],
  );

  const portOf = (node: string, port: string) => {
    if (node.startsWith("room:")) return { domain: "air", direction: "in" };
    const a = edited.assets.get(node);
    return a ? world.types.get(a.type)?.ports?.[port] : undefined;
  };

  const valid = (c: FlowConnection | Edge) => {
    if (!c.source || !c.target || c.source === c.target) return false;
    const from = portOf(c.source, c.sourceHandle ?? "");
    const to = portOf(c.target, c.targetHandle ?? "");
    return Boolean(from && to && from.domain === to.domain);
  };

  const connect = (c: FlowConnection) => {
    const from = portOf(c.source, c.sourceHandle ?? "");
    if (!from || !valid(c)) return;
    const target = c.target.startsWith("room:") ? { node: c.target, port: "air" } : { node: c.target, port: c.targetHandle ?? "" };
    void stage([
      {
        op: "put",
        collection: "connections",
        value: {
          id: connectionId(from.domain, c.source, c.target),
          domain: from.domain,
          source: { node: c.source, port: c.sourceHandle ?? "" },
          target,
        },
      },
    ]);
  };

  const place = (node: Node) => {
    const asset = edited.assets.get(node.id);
    const room = asset?.location?.room ? world.site.rooms?.[asset.location.room] : undefined;
    if (!asset || !room) return;
    const at = planAt(world, node.position.x, node.position.y, graph.offsets, room.floor);
    if (!at) return;
    const moved: Asset = { ...asset, location: { room: at.room, x: at.x, y: at.y } };
    void stage([{ op: "put", collection: "assets", value: moved as unknown as Record<string, unknown> }]);
  };

  return (
    <div className="schematic" data-testid="schematic">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        fitView
        minZoom={0.05}
        colorMode="dark"
        isValidConnection={valid}
        onConnect={connect}
        onNodeDragStop={(_, node) => place(node)}
        onNodeClick={(_, node) => node.type === "asset" && select(node.id)}
        onPaneClick={() => select(null)}
        onEdgesDelete={(gone) => void stage(gone.map((e) => ({ op: "delete", collection: "connections", key: e.id })))}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={SCALE_GRID} size={1} />
        <MiniMap pannable zoomable nodeColor={(n) => (n.type === "room" ? "#1b242d" : "#5fd4ff")} />
        <Controls />
      </ReactFlow>
      <p className="schematic-hint">
        {selected ? `${shortName(selected)} selected. ` : ""}Drag to place, drag outlet to inlet to connect, Delete removes a
        selected connection.
      </p>
    </div>
  );
}

const SCALE_GRID = 24;

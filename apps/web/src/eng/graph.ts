import type { Asset, ComponentType, Connection } from "../api/client";
import type { WorldData } from "../live/world";
import type { Edited } from "./draft";

/** Pixels per metre of plan: the schematic lays assets out where the World Model places them. */
export const SCALE = 24;
const FLOOR_GAP = 14;

export const DOMAIN_COLOUR: Record<string, string> = {
  power: "#ffcf4a",
  chw: "#4fb3ff",
  cw: "#36d6c3",
  air: "#b9c7d4",
  water: "#4f7bff",
  fuel: "#ff8a4f",
  net: "#8fa3b8",
  control: "#c792ff",
  fire: "#ff5a4a",
};

export interface AssetNodeData extends Record<string, unknown> {
  asset: Asset;
  type: ComponentType | undefined;
  role: "scope" | "added" | "context";
  edited: boolean;
}

export interface RoomNodeData extends Record<string, unknown> {
  id: string;
  name: string;
  floor: string;
  width: number;
  height: number;
}

export interface GraphNode {
  id: string;
  kind: "asset" | "room" | "floor";
  x: number;
  y: number;
  data: AssetNodeData | RoomNodeData | { name: string };
}

export interface GraphEdge {
  id: string;
  connection: Connection;
  edited: boolean;
}

/** Each floor's horizontal offset, in metres: floors side by side, lowest first. */
export function floorOffsets(world: WorldData): Map<string, number> {
  const floors = [...(world.site.floors ?? [])].sort((a, b) => a.index - b.index);
  const out = new Map<string, number>();
  let x = 0;
  for (const f of floors) {
    out.set(f.id, x);
    const rooms = Object.values(world.site.rooms ?? {}).filter((r) => r.floor === f.id);
    x += Math.max(10, ...rooms.map((r) => r.x + r.w)) + FLOOR_GAP;
  }
  return out;
}

export function planPosition(world: WorldData, asset: Asset, offsets: Map<string, number>): [number, number] | null {
  const room = asset.location?.room ? world.site.rooms?.[asset.location.room] : undefined;
  if (!room || asset.location?.x == null || asset.location?.y == null) return null;
  return [((offsets.get(room.floor) ?? 0) + asset.location.x) * SCALE, asset.location.y * SCALE];
}

/** Plan coordinates (room, x, y in metres) of a point on the schematic, if it is in a room. */
export function planAt(world: WorldData, px: number, py: number, offsets: Map<string, number>, floor: string) {
  const mx = px / SCALE - (offsets.get(floor) ?? 0);
  const my = py / SCALE;
  const room = Object.values(world.site.rooms ?? {}).find(
    (r) => r.floor === floor && mx >= r.x && mx <= r.x + r.w && my >= r.y && my <= r.y + r.h,
  );
  return room ? { room: room.id, x: Math.round(mx * 10) / 10, y: Math.round(my * 10) / 10 } : null;
}

/**
 * The schematic of a session: the assets it simulates, the ones a draft adds, and the other
 * ends of their connections as context, placed by plan position with their rooms behind them.
 */
export function schematic(world: WorldData, edited: Edited, scope: Set<string>, added: Set<string>) {
  const offsets = floorOffsets(world);
  const shown = new Set<string>([...scope, ...added].filter((id) => edited.assets.has(id)));
  const edges: GraphEdge[] = [];
  for (const c of edited.connections.values()) {
    const ends = [c.source.node, c.target.node];
    if (!ends.some((n) => shown.has(n))) continue;
    edges.push({ id: c.id, connection: c, edited: edited.touched.has(c.id) });
  }
  const context = new Set<string>();
  for (const e of edges)
    for (const n of [e.connection.source.node, e.connection.target.node])
      if (!n.startsWith("room:") && !shown.has(n) && edited.assets.has(n)) context.add(n);

  const nodes: GraphNode[] = [];
  const rooms = new Set<string>();
  for (const id of [...shown, ...context]) {
    const asset = edited.assets.get(id)!;
    const at = planPosition(world, asset, offsets);
    if (asset.location?.room) rooms.add(asset.location.room);
    nodes.push({
      id,
      kind: "asset",
      x: at?.[0] ?? 0,
      y: at?.[1] ?? -4 * SCALE,
      data: {
        asset,
        type: world.types.get(asset.type),
        role: added.has(id) && !scope.has(id) ? "added" : scope.has(id) ? "scope" : "context",
        edited: edited.touched.has(id),
      },
    });
  }
  for (const e of edges)
    for (const n of [e.connection.source.node, e.connection.target.node]) if (n.startsWith("room:")) rooms.add(n.slice(5));
  const floors = new Set<string>();
  for (const id of rooms) {
    const r = world.site.rooms?.[id];
    if (!r) continue;
    floors.add(r.floor);
    nodes.unshift({
      id: `room:${id}`,
      kind: "room",
      x: ((offsets.get(r.floor) ?? 0) + r.x) * SCALE,
      y: r.y * SCALE,
      data: { id, name: r.name, floor: r.floor, width: r.w * SCALE, height: r.h * SCALE },
    });
  }
  for (const f of floors) nodes.unshift({ id: `floor:${f}`, kind: "floor", x: (offsets.get(f) ?? 0) * SCALE, y: -2.4 * SCALE, data: { name: f } });
  // Edges whose ends are both on the schematic.
  const ids = new Set(nodes.map((n) => n.id));
  return { nodes, edges: edges.filter((e) => ids.has(e.connection.source.node) && ids.has(e.connection.target.node)), offsets };
}

/** Ports of `from` and of every node shown that a connection could join, domain by domain. */
export function compatible(
  fromType: ComponentType | undefined,
  port: string,
  candidates: { id: string; type: ComponentType | undefined; room?: boolean }[],
  self: string,
): { node: string; port: string; direction: "to" | "from" }[] {
  const spec = fromType?.ports?.[port];
  if (!spec) return [];
  const out: { node: string; port: string; direction: "to" | "from" }[] = [];
  const canSend = spec.direction !== "in";
  const canReceive = spec.direction !== "out";
  for (const c of candidates) {
    if (c.id === self) continue;
    if (c.room) {
      if (spec.domain === "air" && canSend) out.push({ node: c.id, port: "air", direction: "to" });
      continue;
    }
    for (const [name, p] of Object.entries(c.type?.ports ?? {})) {
      if (p.domain !== spec.domain) continue;
      if (canSend && p.direction !== "out") out.push({ node: c.id, port: name, direction: "to" });
      else if (canReceive && p.direction !== "in") out.push({ node: c.id, port: name, direction: "from" });
    }
  }
  return out;
}

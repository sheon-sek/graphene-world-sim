/**
 * The whole site in 3D, derived from World Model data only: its floors stacked at their
 * elevations, every room as a floor slab with low walls, and every asset where the World
 * Model places it (#95). Scene axes: x is the site's x, z is its y, y is up, in metres.
 *
 * Each asset type maps to a silhouette family (a box or a cylinder of a typical size) and a
 * system colour. Assets the World Model does not place (breakers, dashboards) are set beside
 * the placed asset they connect to, or in a row at the edge of the site.
 */

export type Shape = "box" | "cylinder";
export type System = "cooling" | "air" | "electrical" | "fire" | "network" | "water" | "sensor" | "it" | "control";

export interface SiteFloor {
  id: string;
  index: number;
  elevation: number;
  height: number;
}

export interface SiteRoom {
  id: string;
  name: string;
  floor: string;
  kind: string;
  x: number;
  y: number;
  w: number;
  h: number;
  outdoor: boolean;
}

export interface SiteAsset {
  id: string;
  name: string;
  type: string;
  room: string | null;
  floor: string;
  shape: Shape;
  system: System;
  /** Centre of the base. */
  position: [number, number, number];
  /** Width (x), depth (z), height (y). */
  size: [number, number, number];
  placed: boolean;
}

export interface SiteLayout {
  floors: SiteFloor[];
  rooms: SiteRoom[];
  assets: SiteAsset[];
  width: number;
  depth: number;
}

interface Silhouette {
  shape: Shape;
  size: [number, number, number];
  system: System;
  /** Mounted this high above the floor (sensors and detectors). */
  lift?: number;
}

const S = (shape: Shape, w: number, d: number, h: number, system: System, lift?: number): Silhouette => ({
  shape,
  size: [w, d, h],
  system,
  lift,
});

const SILHOUETTES: [RegExp, Silhouette][] = [
  [/^Chiller$/, S("box", 6, 2.6, 2.6, "cooling")],
  [/Cooling Tower/, S("box", 4.2, 4.2, 4.5, "cooling")],
  [/Buffer Tank|CW Ground Tank|CW Roof Tank/, S("cylinder", 2.6, 2.6, 3.4, "water")],
  [/Chiller Pump|CW .*Pump|Makeup|AC Makeup|Fire Pump/, S("box", 1.2, 0.8, 1, "water")],
  [/Valve/, S("box", 0.6, 0.6, 0.8, "water")],
  [/Cooling Block/, S("box", 2.4, 1.2, 2.2, "cooling")],
  [/Ceiling Cooling Units/, S("box", 3, 1.2, 0.5, "cooling", 3.6)],
  [/^FCU$|^FWU$|^CRAC$|^PAHU$/, S("box", 2, 1, 2.1, "air")],
  [/^CDU$/, S("box", 1.2, 1, 2, "cooling")],
  [/^UPS$/, S("box", 1.8, 1, 2, "electrical")],
  [/Genset/, S("box", 7, 2.6, 3, "electrical")],
  [/Diesel/, S("cylinder", 3, 3, 2.6, "electrical")],
  [/Utility Supply/, S("box", 3, 2.4, 2.6, "electrical")],
  [/^ATS$|Breaker|IPS|Small Power|Demo Load/, S("box", 0.8, 0.5, 1.8, "electrical")],
  [/BCPM|GPM|GPQM|GEM|E820|GDC|RCMS/, S("box", 0.5, 0.25, 0.7, "electrical", 1.2)],
  [/^IT Load$/, S("box", 4, 4, 2.2, "it")],
  [/Network|Switch/, S("box", 0.6, 0.6, 2, "network")],
  [/Smoke|Heat Detector/, S("cylinder", 0.3, 0.3, 0.15, "fire", 4)],
  [/Manual Call Point|Alarm Valve|Fire Zone/, S("box", 0.4, 0.3, 0.4, "fire", 1.2)],
  [/Water Leak/, S("box", 2, 0.1, 0.05, "water")],
  [/Temperature and Humidity|Environment Monitoring|Weather/, S("box", 0.25, 0.15, 0.25, "sensor", 1.6)],
  [/Lift/, S("box", 2.2, 2.2, 2.6, "control")],
  [/Controller|Dashboard|DI Event|view/i, S("box", 0.8, 0.5, 1.6, "control")],
];

const DEFAULT = S("box", 1, 1, 1, "control");

export function silhouetteOf(type: string): Silhouette {
  return SILHOUETTES.find(([pattern]) => pattern.test(type))?.[1] ?? DEFAULT;
}

/** Colours by system, used as the base colour of a healthy asset. */
export const SYSTEM_COLOUR: Record<System, string> = {
  cooling: "#4aa8ff",
  air: "#6fd3e8",
  electrical: "#f2b544",
  fire: "#ff7a6b",
  network: "#b38cff",
  water: "#3fc4a8",
  sensor: "#c7d1db",
  it: "#8fa3b8",
  control: "#d9a6ff",
};

export interface LayoutInput {
  floors: { id: string; index?: number | null; elevation_m?: number | null; height_m?: number | null }[];
  rooms: Record<string, Partial<SiteRoom> & { id: string; floor: string; x: number; y: number; w: number; h: number }>;
  assets: { id: string; name: string; type: string; location?: { room?: string | null; x?: number | null; y?: number | null } | null }[];
  /** Pairs of asset ids that a connection joins, to set unplaced assets beside a neighbour. */
  links: [string, string][];
}

export function layoutSite(input: LayoutInput): SiteLayout {
  const floors: SiteFloor[] = input.floors
    .map((f, i) => ({ id: f.id, index: f.index ?? i, elevation: f.elevation_m ?? i * 4.5, height: f.height_m ?? 4.5 }))
    .sort((a, b) => a.index - b.index);
  const floorOf = new Map(floors.map((f) => [f.id, f]));
  const rooms: SiteRoom[] = Object.values(input.rooms).map((r) => ({
    id: r.id,
    name: r.name ?? r.id,
    floor: r.floor,
    kind: r.kind ?? "",
    x: r.x,
    y: r.y,
    w: r.w,
    h: r.h,
    outdoor: Boolean(r.outdoor),
  }));
  const roomOf = new Map(rooms.map((r) => [r.id, r]));
  const width = Math.max(...rooms.map((r) => r.x + r.w), 1);
  const depth = Math.max(...rooms.map((r) => r.y + r.h), 1);

  const placed = new Map<string, SiteAsset>();
  const unplaced: LayoutInput["assets"] = [];
  for (const a of input.assets) {
    const loc = a.location;
    const room = loc?.room ? roomOf.get(loc.room) : undefined;
    if (!room || loc?.x == null || loc?.y == null) {
      unplaced.push(a);
      continue;
    }
    const sil = silhouetteOf(a.type);
    const floor = floorOf.get(room.floor);
    const base = (floor?.elevation ?? 0) + (sil.lift ?? 0);
    placed.set(a.id, {
      id: a.id,
      name: a.name,
      type: a.type,
      room: room.id,
      floor: room.floor,
      shape: sil.shape,
      system: sil.system,
      position: [loc.x, base, loc.y],
      size: [...sil.size],
      placed: true,
    });
  }

  // Spread assets that share a spot (a sensor on a unit, several meters on one board).
  const spots = new Map<string, SiteAsset[]>();
  for (const a of placed.values()) {
    const key = `${a.floor}|${a.position[0].toFixed(1)}|${a.position[2].toFixed(1)}`;
    spots.set(key, [...(spots.get(key) ?? []), a]);
  }
  for (const group of spots.values()) {
    if (group.length < 2) continue;
    group.sort((a, b) => a.id.localeCompare(b.id));
    const ring = Math.max(...group.map((a) => Math.max(a.size[0], a.size[1]))) * 0.6 + 0.3;
    group.forEach((a, i) => {
      if (i === 0) return;
      const angle = (i / group.length) * Math.PI * 2;
      a.position = [a.position[0] + Math.cos(angle) * ring, a.position[1], a.position[2] + Math.sin(angle) * ring];
    });
  }

  const neighbours = new Map<string, string[]>();
  for (const [a, b] of input.links) {
    neighbours.set(a, [...(neighbours.get(a) ?? []), b]);
    neighbours.set(b, [...(neighbours.get(b) ?? []), a]);
  }
  const besideCount = new Map<string, number>();
  let spare = 0;
  const ground = floors[0];
  for (const a of unplaced.sort((x, y) => x.id.localeCompare(y.id))) {
    const sil = silhouetteOf(a.type);
    const anchor = (neighbours.get(a.id) ?? []).map((n) => placed.get(n)).find((n) => n !== undefined);
    let position: [number, number, number];
    let floor = ground?.id ?? "";
    let room: string | null = null;
    if (anchor) {
      const n = besideCount.get(anchor.id) ?? 0;
      besideCount.set(anchor.id, n + 1);
      floor = anchor.floor;
      room = anchor.room;
      const elevation = floorOf.get(floor)?.elevation ?? 0;
      position = [anchor.position[0] + anchor.size[0] / 2 + 0.6 + n * 0.9, elevation, anchor.position[2] - anchor.size[1] / 2 - 0.4];
    } else {
      // A row along the front of the site, outside the building.
      position = [2 + (spare % 40) * 2.4, ground?.elevation ?? 0, -6 - Math.floor(spare / 40) * 2.4];
      spare += 1;
    }
    placed.set(a.id, {
      id: a.id,
      name: a.name,
      type: a.type,
      room,
      floor,
      shape: sil.shape,
      system: sil.system,
      position,
      size: [...sil.size],
      placed: false,
    });
  }

  return { floors, rooms, assets: [...placed.values()], width, depth };
}

/** Floors above `floor` are cut away when looking at it. */
export function visibleFloors(layout: SiteLayout, floor: string | null): Set<string> {
  if (floor === null) return new Set(layout.floors.map((f) => f.id));
  const level = layout.floors.find((f) => f.id === floor)?.index ?? Infinity;
  return new Set(layout.floors.filter((f) => f.index <= level).map((f) => f.id));
}

export interface ConnectionInput {
  id: string;
  domain: string;
  source: { node: string };
  target: { node: string };
}

export interface ShaftInput {
  x: number;
  y: number;
  carries?: string[] | null;
}

export interface Route {
  id: string;
  domain: string;
  from: string;
  to: string;
  /** A polyline, in scene coordinates. */
  points: [number, number, number][];
}

/** Height above the floor of each domain's ceiling tray, so the layers do not overlap. */
const TRAY: Record<string, number> = { power: 3.9, net: 4.1, fire: 4.2, chw: 3.5, cw: 3.4, water: 3.2, air: 3.0, fuel: 0.3 };

export const DOMAIN_COLOUR: Record<string, string> = {
  power: "#f2b544",
  fuel: "#c98a3a",
  chw: "#4aa8ff",
  cw: "#2fd3c0",
  air: "#9be7ff",
  water: "#3fc4a8",
  net: "#b38cff",
  fire: "#ff7a6b",
};

/**
 * Connections drawn orthogonally along ceiling trays, one height per domain. A connection
 * between floors runs to its domain's riser shaft, up or down it, and across to the other end.
 */
export function routeConnections(
  layout: SiteLayout,
  connections: ConnectionInput[],
  shafts: Record<string, ShaftInput>,
): Route[] {
  const assets = new Map(layout.assets.map((a) => [a.id, a]));
  const rooms = new Map(layout.rooms.map((r) => [r.id, r]));
  const floors = new Map(layout.floors.map((f) => [f.id, f]));
  const end = (node: string): { at: [number, number, number]; floor: string } | null => {
    if (node.startsWith("room:")) {
      const r = rooms.get(node.slice(5));
      const f = r && floors.get(r.floor);
      return r && f ? { at: [r.x + r.w / 2, f.elevation, r.y + r.h / 2], floor: r.floor } : null;
    }
    const a = assets.get(node);
    if (!a) return null;
    const f = floors.get(a.floor);
    return { at: [a.position[0], f?.elevation ?? a.position[1], a.position[2]], floor: a.floor };
  };
  const routes: Route[] = [];
  for (const c of connections) {
    const a = end(c.source.node);
    const b = end(c.target.node);
    if (!a || !b) continue;
    const tray = TRAY[c.domain] ?? 3.6;
    const ya = a.at[1] + tray;
    const yb = b.at[1] + tray;
    const pts: [number, number, number][] = [a.at, [a.at[0], ya, a.at[2]]];
    if (a.floor === b.floor) {
      pts.push([b.at[0], ya, a.at[2]], [b.at[0], ya, b.at[2]]);
    } else {
      const shaft = Object.values(shafts).find((s) => s.carries?.includes(c.domain)) ?? { x: layout.width / 2, y: layout.depth / 2 };
      pts.push([shaft.x, ya, a.at[2]], [shaft.x, ya, shaft.y], [shaft.x, yb, shaft.y], [b.at[0], yb, shaft.y], [b.at[0], yb, b.at[2]]);
    }
    pts.push(b.at);
    routes.push({ id: c.id, domain: c.domain, from: c.source.node, to: c.target.node, points: pts });
  }
  return routes;
}

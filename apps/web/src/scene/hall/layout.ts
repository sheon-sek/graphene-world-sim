/**
 * The layout of a data hall, derived from World Model data only: the room, its floor and the
 * assets placed in it. Nothing here names a particular asset, so a new placement appears in
 * the scene without code changes (#50).
 *
 * Scene axes: x is the room's x, z is the room's y, y is up (metres, origin at the room's
 * corner on its floor). The World Model has no rack placements, so rack rows are generated
 * from the hot-aisle sensors placed in the hall, or evenly when there are none.
 */

export interface Room {
  id: string;
  name: string;
  floor: string;
  kind: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Floor {
  id: string;
  elevation_m: number;
  height_m: number;
}

export interface PlacedAsset {
  id: string;
  name: string;
  type: string;
  x: number;
  y: number;
  in_scope: boolean;
  /** Numeric engineering parameters, the asset's own values over its type's defaults. */
  parameters?: Record<string, number>;
}

export interface HallWorld {
  room: Room;
  floor: Floor;
  assets: PlacedAsset[];
}

export type UnitKind =
  | "fan-coil"
  | "ceiling-coils"
  | "perimeter"
  | "cooling-block"
  | "wall-panel"
  | "sensor"
  | "ceiling-device"
  | "floor-cable"
  | "it-load";

export interface SceneAsset extends PlacedAsset {
  kind: UnitKind;
  /** Scene position of the asset's base (or, for ceiling devices, its mounting point). */
  position: [number, number, number];
  /** Rotation about y, radians: the direction the unit faces into the hall. */
  facing: number;
  /** Footprint for picking and camera framing (w along x, d along z, h up). */
  size: [number, number, number];
}

export interface RackRow {
  /** Row centre line along z, and the side its fronts face (-1: towards -z, +1: towards +z). */
  z: number;
  front: -1 | 1;
  x0: number;
  count: number;
}

export interface HotAisle {
  z: number;
  x0: number;
  x1: number;
}

export interface HallLayout {
  width: number;
  depth: number;
  height: number;
  racks: RackRow[];
  hotAisles: HotAisle[];
  assets: SceneAsset[];
  rackCount: number;
}

export const RACK = { w: 0.6, d: 1.2, h: 2.2 } as const;
export const HOT_AISLE_WIDTH = 1.2;
/** Clear height of the hall below the service zone. */
const CLEAR_HEIGHT = 3.6;
/** Keep rack rows this far from walls, and out of the strip perimeter units stand in. */
const WALL_GAP = 1.2;
const PERIMETER_STRIP = 3.0;

const KIND_BY_TYPE: Record<string, UnitKind> = {
  FCU: "fan-coil",
  CRAC: "perimeter",
  PAHU: "perimeter",
  FWU: "perimeter",
  "Ceiling Cooling Units": "ceiling-coils",
  "Cooling Block": "cooling-block",
  BCPM: "wall-panel",
  "Temperature and Humidity": "sensor",
  "Environment Monitoring": "sensor",
  "Heat Detector": "ceiling-device",
  "Smoke Detector": "ceiling-device",
  "Manual Call Point": "wall-panel",
  "Fire Zone": "ceiling-device",
  "Water Leak Cable Sensor": "floor-cable",
  "IT Load": "it-load",
};

export function kindOf(type: string): UnitKind {
  return KIND_BY_TYPE[type] ?? "perimeter";
}

const SIZE: Record<UnitKind, [number, number, number]> = {
  "fan-coil": [2.1, 0.9, 2.1],
  perimeter: [2.1, 0.9, 2.0],
  "ceiling-coils": [9.0, 1.0, 0.45],
  "cooling-block": [1.6, 0.8, 1.9],
  "wall-panel": [0.8, 0.25, 1.2],
  sensor: [0.12, 0.12, 0.12],
  "ceiling-device": [0.18, 0.18, 0.08],
  "floor-cable": [0.3, 0.3, 0.05],
  "it-load": [0, 0, 0],
};

/** The rack rows and hot aisles: two rows back to back around each hot aisle. */
function rackRows(world: HallWorld, perimeterX: number | null): { rows: RackRow[]; aisles: HotAisle[] } {
  const { w, h } = world.room;
  const x0 = WALL_GAP;
  const x1 = (perimeterX ?? w) - (perimeterX === null ? WALL_GAP : PERIMETER_STRIP - 0.6);
  const count = Math.max(0, Math.floor((x1 - x0) / RACK.w));
  const span = count * RACK.w;
  const start = x0 + (x1 - x0 - span) / 2;
  const half = HOT_AISLE_WIDTH / 2 + RACK.d / 2;
  let centres = world.assets
    .filter((a) => a.type === "Temperature and Humidity")
    .map((a) => a.y)
    .filter((y, i, all) => all.indexOf(y) === i)
    .sort((a, b) => a - b);
  if (centres.length === 0) {
    const pitch = 2 * (RACK.d + HOT_AISLE_WIDTH);
    for (let z = WALL_GAP + 1.2 + half + RACK.d / 2; z + half + RACK.d / 2 < h - WALL_GAP; z += pitch) {
      centres.push(z);
    }
  }
  centres = centres.filter((z) => z - half - RACK.d / 2 >= 0.6 && z + half + RACK.d / 2 <= h - 0.6);
  const rows: RackRow[] = [];
  const aisles: HotAisle[] = [];
  for (const z of centres) {
    // Backs face the hot aisle, so the fronts face away from it.
    rows.push({ z: z - half, front: -1, x0: start, count });
    rows.push({ z: z + half, front: 1, x0: start, count });
    aisles.push({ z, x0: start, x1: start + span });
  }
  return { rows, aisles };
}

function nearestWall(x: number, z: number, w: number, d: number): { x: number; z: number; facing: number } {
  const options = [
    { dist: z, x, z: 0.15, facing: 0 },
    { dist: d - z, x, z: d - 0.15, facing: Math.PI },
    { dist: x, x: 0.15, z, facing: Math.PI / 2 },
    { dist: w - x, x: w - 0.15, z, facing: -Math.PI / 2 },
  ];
  options.sort((a, b) => a.dist - b.dist);
  return options[0];
}

export function layoutHall(world: HallWorld): HallLayout {
  const { w, h } = world.room;
  const height = Math.min(CLEAR_HEIGHT, world.floor.height_m - 0.6);
  const perimeter = world.assets.filter((a) => {
    const k = kindOf(a.type);
    return (k === "fan-coil" || k === "perimeter") && a.x > w - PERIMETER_STRIP;
  });
  const perimeterX = perimeter.length ? Math.min(...perimeter.map((a) => a.x)) - 0.6 : null;
  const { rows, aisles } = rackRows(world, perimeterX);

  const assets: SceneAsset[] = world.assets.map((a) => {
    const kind = kindOf(a.type);
    const size: [number, number, number] = [...SIZE[kind]];
    let position: [number, number, number] = [a.x, 0, a.y];
    let facing = 0;
    if (kind === "fan-coil" || kind === "perimeter" || kind === "cooling-block") {
      const wall = nearestWall(a.x, a.y, w, h);
      facing = wall.facing;
      const inset = size[1] / 2 + 0.2;
      position = [
        wall.facing === Math.PI / 2 ? inset : wall.facing === -Math.PI / 2 ? w - inset : a.x,
        0,
        wall.facing === 0 ? inset : wall.facing === Math.PI ? h - inset : a.y,
      ];
    } else if (kind === "wall-panel") {
      const wall = nearestWall(a.x, a.y, w, h);
      facing = wall.facing;
      position = [wall.x, 1.1, wall.z];
    } else if (kind === "ceiling-coils") {
      const aisle = aisles.reduce<HotAisle | null>(
        (best, x) => (best === null || Math.abs(x.z - a.y) < Math.abs(best.z - a.y) ? x : best),
        null,
      );
      position = [aisle ? (aisle.x0 + aisle.x1) / 2 : a.x, height - 0.35, aisle ? aisle.z : a.y];
      if (aisle) size[0] = Math.max(2, aisle.x1 - aisle.x0 - 1.2);
    } else if (kind === "sensor") {
      position = [a.x, 1.6, a.y];
    } else if (kind === "ceiling-device") {
      position = [a.x, height, a.y];
    }
    return { ...a, kind, position, facing, size };
  });

  return {
    width: w,
    depth: h,
    height,
    racks: rows,
    hotAisles: aisles,
    assets,
    rackCount: rows.reduce((n, r) => n + r.count, 0),
  };
}

/** The asset a rack belongs to: the hall's IT load, when the World Model places one. */
export function itLoadOf(layout: HallLayout): SceneAsset | undefined {
  return layout.assets.find((a) => a.kind === "it-load");
}

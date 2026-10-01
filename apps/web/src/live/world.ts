import { useEffect, useState } from "react";
import { api, must, type Asset, type ComponentType, type Connection, type Schemas, type Site } from "../api/client";
import type { HallWorld, PlacedAsset } from "../scene/hall/layout";

/** One World Model revision as the workspaces read it: small enough to load whole. */
export interface WorldData {
  revision: number;
  site: Site;
  types: Map<string, ComponentType>;
  assets: Map<string, Asset>;
  connections: Map<string, Connection>;
}

const cache = new Map<number, Promise<WorldData>>();

export function loadWorld(revision: number): Promise<WorldData> {
  let found = cache.get(revision);
  if (!found) {
    const query = { params: { query: { revision } } };
    found = Promise.all([
      must(api.GET("/api/world-model/site", query)),
      must(api.GET("/api/world-model/types", query)),
      must(api.GET("/api/world-model/assets", query)),
      must(api.GET("/api/world-model/connections", query)),
    ]).then(([site, types, assets, connections]) => ({
      revision,
      site,
      types: new Map(types.map((t) => [t.id, t])),
      assets: new Map(assets.map((a) => [a.id, a])),
      connections: new Map(connections.map((c) => [c.id, c])),
    }));
    found.catch(() => cache.delete(revision));
    cache.set(revision, found);
  }
  return found;
}

export function useWorld(revision: number | null | undefined): { world: WorldData | null; error: string | null } {
  const [state, setState] = useState<{ world: WorldData | null; error: string | null }>({ world: null, error: null });
  useEffect(() => {
    if (!revision) return;
    let alive = true;
    loadWorld(revision).then(
      (world) => alive && setState({ world, error: null }),
      (e: unknown) => alive && setState({ world: null, error: String(e) }),
    );
    return () => {
      alive = false;
    };
  }, [revision]);
  return state;
}

function numeric(values: Record<string, unknown>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(values)) if (typeof v === "number") out[k] = v;
  return out;
}

/** An asset's parameters: its own values over its type's defaults. */
export function parametersOf(world: WorldData, asset: Asset): Record<string, unknown> {
  const spec = world.types.get(asset.type)?.parameters ?? {};
  const defaults = Object.fromEntries(Object.entries(spec).map(([k, p]) => [k, p.default]));
  return { ...defaults, ...(asset.parameters ?? {}) };
}

/** The hall a session looks at: the `hall` room holding most of its scope. */
export function sessionRoom(world: WorldData, scope: Iterable<string>): string | null {
  const count = new Map<string, number>();
  for (const id of scope) {
    const room = world.assets.get(id)?.location?.room;
    if (room && world.site.rooms?.[room]?.kind === "hall") count.set(room, (count.get(room) ?? 0) + 1);
  }
  let best: string | null = null;
  for (const [room, n] of count) if (best === null || n > (count.get(best) ?? 0)) best = room;
  return best;
}

export function hallOf(world: WorldData, room: string, scope: Set<string>): HallWorld | null {
  const r = world.site.rooms?.[room];
  if (!r) return null;
  const floor = world.site.floors?.find((f) => f.id === r.floor);
  if (!floor) return null;
  const placed: PlacedAsset[] = [];
  for (const a of world.assets.values()) {
    const loc = a.location;
    if (loc?.room !== room || loc.x == null || loc.y == null) continue;
    placed.push({
      id: a.id,
      name: a.name,
      type: a.type,
      // The World Model places assets in site coordinates; the hall is drawn from its corner.
      x: loc.x - (r.x ?? 0),
      y: loc.y - (r.y ?? 0),
      in_scope: scope.has(a.id),
      parameters: numeric(parametersOf(world, a)),
    });
  }
  return {
    room: { ...r, fire_zone: undefined } as HallWorld["room"],
    floor: { id: floor.id, elevation_m: floor.elevation_m ?? 0, height_m: floor.height_m ?? 4.5 },
    assets: placed,
  };
}

export type PointBinding = Schemas["PointBinding-Output"];

/** Every point of a revision, and which asset each one belongs to. */
export interface PointIndex {
  bindings: Map<string, PointBinding>;
  /** Export path → the asset it measures or commands. */
  owner: Map<string, string>;
  /** Asset → its points, in path order. */
  byAsset: Map<string, string[]>;
  /** Asset → its fault and alarm points. */
  alarms: Map<string, string[]>;
  /** Points no asset owns (site dashboards, plant views). */
  loose: string[];
}

const pointCache = new Map<number, Promise<PointIndex>>();

/** The asset a point belongs to: the asset its source reads, the asset its instrument is on,
 * or the asset whose id is the longest prefix of its path. */
export function indexPoints(
  bindings: PointBinding[],
  assetIds: Iterable<string>,
  instrumentAsset: Map<string, string>,
): PointIndex {
  const ids = [...assetIds].sort((a, b) => b.length - a.length);
  const index: PointIndex = { bindings: new Map(), owner: new Map(), byAsset: new Map(), alarms: new Map(), loose: [] };
  for (const b of [...bindings].sort((x, y) => x.path.localeCompare(y.path))) {
    index.bindings.set(b.path, b);
    const src = b.source as { kind: string; asset?: string; instrument?: string } | undefined;
    let owner: string | undefined;
    if (src?.kind === "asset_signal") owner = src.asset;
    else if (src?.kind === "instrument" && src.instrument) owner = instrumentAsset.get(src.instrument);
    owner ??= ids.find((id) => b.path.startsWith(`${id}/`));
    if (!owner) {
      index.loose.push(b.path);
      continue;
    }
    index.owner.set(b.path, owner);
    const list = index.byAsset.get(owner) ?? [];
    list.push(b.path);
    index.byAsset.set(owner, list);
    if (b.point_class === "fault_alarm") {
      const alarms = index.alarms.get(owner) ?? [];
      alarms.push(b.path);
      index.alarms.set(owner, alarms);
    }
  }
  return index;
}

export function loadPoints(world: WorldData): Promise<PointIndex> {
  let found = pointCache.get(world.revision);
  if (!found) {
    const revision = world.revision;
    found = (async () => {
      const bindings: PointBinding[] = [];
      for (let offset = 0; ; offset += 1000) {
        const page = await must(
          api.GET("/api/world-model/points", { params: { query: { revision, offset, limit: 1000 } } }),
        );
        bindings.push(...page.items);
        if (bindings.length >= page.total || page.items.length === 0) break;
      }
      const instruments = await must(api.GET("/api/world-model/instruments", { params: { query: { revision } } }));
      const instrumentAsset = new Map(instruments.map((i) => [i.id, i.asset]));
      return indexPoints(bindings, world.assets.keys(), instrumentAsset);
    })();
    found.catch(() => pointCache.delete(revision));
    pointCache.set(revision, found);
  }
  return found;
}

export function usePoints(world: WorldData | null): PointIndex | null {
  const [index, setIndex] = useState<PointIndex | null>(null);
  useEffect(() => {
    if (!world) return;
    let alive = true;
    loadPoints(world).then(
      (i) => alive && setIndex(i),
      () => alive && setIndex(null),
    );
    return () => {
      alive = false;
    };
  }, [world]);
  return index;
}

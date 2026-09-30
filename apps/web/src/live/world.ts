import { useEffect, useState } from "react";
import { api, must, type Asset, type ComponentType, type Connection, type Site } from "../api/client";
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
      x: loc.x,
      y: loc.y,
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

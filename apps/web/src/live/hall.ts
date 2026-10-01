import type { HallWorld, PlacedAsset } from "../scene/hall/layout";
import { LiveSource, ReplaySource, streamUrl, type Frame, type FrameSource } from "./frames";

/** A recorded trajectory: the hall it happened in and every frame (`fixtures/record.py`). */
export interface Recording {
  world: HallWorld;
  scenario: string;
  dt: number;
  revision: number;
  scope: string[];
  frames: Frame[];
}

export interface LoadedHall {
  world: HallWorld;
  source: FrameSource;
  title: string;
}

export async function loadRecording(url: string, speed = 10): Promise<LoadedHall> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: ${response.status}`);
  const recording = (await response.json()) as Recording;
  const source = new ReplaySource(recording.frames, speed);
  source.startClock();
  return { world: recording.world, source, title: recording.scenario };
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`${url}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

interface ApiAsset {
  id: string;
  name: string;
  type: string;
  parameters: Record<string, unknown>;
  location: { room: string | null; x: number | null; y: number | null };
}

interface ApiType {
  id: string;
  parameters: Record<string, { default?: unknown }>;
}

interface ApiSite {
  rooms: Record<string, HallWorld["room"]>;
  floors: HallWorld["floor"][];
}

interface ApiSession {
  id: string;
  revision: number | null;
  scope: string[];
}

function numeric(values: Record<string, unknown>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(values)) if (typeof v === "number") out[k] = v;
  return out;
}

/** A room of a live session's World Model revision, and the session's frame stream. */
export async function loadLiveHall(api: string, session: string, room: string): Promise<LoadedHall> {
  const base = api.replace(/\/$/, "");
  const live = await getJson<ApiSession>(`${base}/runtime/sessions/${encodeURIComponent(session)}`);
  const revision = live.revision ? `?revision=${live.revision}` : "";
  const join = revision ? "&" : "?";
  const [site, assets, types] = await Promise.all([
    getJson<ApiSite>(`${base}/world-model/site${revision}`),
    getJson<ApiAsset[]>(`${base}/world-model/assets${revision}${join}room=${encodeURIComponent(room)}`),
    getJson<ApiType[]>(`${base}/world-model/types${revision}`),
  ]);
  const hallRoom = site.rooms[room];
  if (!hallRoom) throw new Error(`no room ${room} in revision ${live.revision}`);
  const floor = site.floors.find((f) => f.id === hallRoom.floor);
  if (!floor) throw new Error(`no floor ${hallRoom.floor}`);
  const typeById = new Map(types.map((t) => [t.id, t]));
  const scope = new Set(live.scope);
  const placed: PlacedAsset[] = assets
    .filter((a) => a.location.x !== null && a.location.y !== null)
    .map((a) => {
      const defaults = Object.fromEntries(
        Object.entries(typeById.get(a.type)?.parameters ?? {}).map(([k, p]) => [k, p.default]),
      );
      return {
        id: a.id,
        name: a.name,
        type: a.type,
        x: (a.location.x ?? 0) - hallRoom.x,
        y: (a.location.y ?? 0) - hallRoom.y,
        in_scope: scope.has(a.id),
        parameters: numeric({ ...defaults, ...a.parameters }),
      };
    });
  return {
    world: { room: hallRoom, floor, assets: placed },
    source: new LiveSource(streamUrl(base, session)),
    title: `Session ${session}, revision ${live.revision ?? "head"}`,
  };
}

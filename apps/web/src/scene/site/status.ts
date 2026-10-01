import type { Frame } from "../../live/frames";
import type { PointIndex } from "../../live/world";

/**
 * A first reading of an asset's condition from the frame, for colouring the site: a fault
 * injected on it, a fault or alarm point of it that is set, a point of it with bad quality,
 * healthy, or outside the session's scope (not simulated). The simulation decides all of
 * these; this only reads them.
 */
export type AssetStatus = "fault" | "alarm" | "bad" | "ok" | "idle";

export const STATUS_COLOUR: Record<Exclude<AssetStatus, "ok">, string> = {
  fault: "#ff3b30",
  alarm: "#ff9f0a",
  bad: "#d65cff",
  idle: "#3a4552",
};

function set(value: unknown): boolean {
  return value === true || (typeof value === "number" && value !== 0);
}

export function statusOf(
  id: string,
  frame: Frame | null,
  points: PointIndex | null,
  scope: Set<string>,
  faulted: Set<string>,
): AssetStatus {
  if (!scope.has(id)) return "idle";
  if (faulted.has(id)) return "fault";
  if (!frame || !points) return "ok";
  for (const path of points.alarms.get(id) ?? []) if (set(frame.points[path]?.value)) return "alarm";
  for (const path of points.byAsset.get(id) ?? []) {
    const p = frame.points[path];
    if (p && p.quality === "bad" && p.reason !== "not_simulated") return "bad";
  }
  return "ok";
}

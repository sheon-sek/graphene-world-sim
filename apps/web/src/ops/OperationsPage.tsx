import { lazy, Suspense, useMemo, useState } from "react";
import { useSession } from "../app/session";
import { useSim } from "../live/store";
import { hallOf, sessionRoom, usePoints, useWorld } from "../live/world";
import type { ViewOptions } from "../scene/HallView";
import { initialQuality, saveQuality, type Quality } from "../scene/quality";
import { SceneBoundary } from "../scene/SceneBoundary";
import type { SiteOptions } from "../scene/site/SiteView";
import { AlarmsPanel } from "./Alarms";
import { FaultPanel } from "./FaultPanel";
import { Inspector } from "./Inspector";
import { Plan } from "./Plan";
import { Propagation } from "./Propagation";

// three.js loads only when a 3D view is shown.
const HallView = lazy(() => import("../scene/HallView").then((m) => ({ default: m.HallView })));
const SiteView = lazy(() => import("../scene/site/SiteView").then((m) => ({ default: m.SiteView })));

const VIEW: ViewOptions = { airField: true, ambientOcclusion: true, bloom: true };

type Mode = "site" | "hall" | "plan";

/** Connection layers, as the site view draws them. */
const LAYERS: [string, string, string[]][] = [
  ["electrical", "Electrical", ["power", "fuel"]],
  ["hydraulic", "Chilled & condenser water", ["chw", "cw"]],
  ["air", "Airside", ["air"]],
  ["network", "Network", ["net"]],
  ["fire", "Fire", ["fire"]],
  ["water", "Water", ["water"]],
];

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function store(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* a private window keeps the choice for this page only */
  }
}

function initialMode(): Mode {
  const q = new URLSearchParams(window.location.search);
  if (q.get("3d") === "0") return "plan";
  const asked = q.get("view") ?? stored("gws.mode");
  return asked === "hall" || asked === "plan" || asked === "site" ? asked : "site";
}

/** Watch the site or a hall, inspect assets and points, follow alarms, inject faults and
 * trace their consequences. */
export function OperationsPage({ sid }: { sid: string }) {
  const revision = useSession((s) => s.info?.revision);
  // The session's description changes with every poll while it runs (its clock moves); the
  // views depend only on its revision and scope, so they are rebuilt only when one changes.
  const scopeKey = useSession((s) => s.info?.scope.join("\n"));
  const scope = useMemo(() => new Set(scopeKey ? scopeKey.split("\n") : []), [scopeKey]);
  const { world, error } = useWorld(revision);
  const points = usePoints(world);
  const selected = useSim((s) => s.selected);
  const [mode, setModeState] = useState<Mode>(initialMode);
  const setMode = (m: Mode) => {
    store("gws.mode", m);
    setModeState(m);
  };
  const [{ quality, reason }, setQuality] = useState(initialQuality);
  const chooseQuality = (q: Quality) => {
    saveQuality(q);
    setQuality({ quality: q, reason: "your choice" });
  };
  const stepDown = (fps: number) => {
    saveQuality("low");
    setQuality({ quality: "low", reason: `stepped down at ${Math.round(fps)} fps` });
  };
  const [room, setRoom] = useState<string | null>(() => new URLSearchParams(window.location.search).get("room"));
  const [floor, setFloor] = useState<string | null>(null);
  const [explode, setExplode] = useState(true);
  const [layers, setLayers] = useState<Set<string>>(new Set());
  const [hover, setHover] = useState<string | null>(null);

  const halls = useMemo(
    () =>
      Object.values(world?.site.rooms ?? {})
        .filter((r) => r.kind === "hall")
        .sort((a, b) => a.id.localeCompare(b.id)),
    [world],
  );
  const hallId = room ?? (world ? sessionRoom(world, scope) : null) ?? halls[0]?.id ?? null;
  const hall = useMemo(() => (world && hallId ? hallOf(world, hallId, scope) : null), [world, hallId, scope]);
  const selectedRoom = selected ? world?.assets.get(selected)?.location?.room : null;
  const selectedHall = halls.find((h) => h.id === selectedRoom);
  const siteOptions = useMemo<SiteOptions>(
    () => ({ floor, explode, layers: new Set(LAYERS.filter(([k]) => layers.has(k)).flatMap(([, , d]) => d)) }),
    [floor, explode, layers],
  );
  const toggleLayer = (k: string) =>
    setLayers((l) => {
      const next = new Set(l);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const title =
    mode === "site" ? (world?.site.name ?? "Site") : hall ? hall.room.name : error ? `Could not load: ${error}` : "Loading…";
  const three = mode !== "plan";

  return (
    <div className="ops">
      <AlarmsPanel sid={sid} world={world} points={points} scope={scope} />
      <section className="view" data-testid="hall-view">
        <div className="view-bar">
          <span className="view-title">
            <span className="seg" role="tablist" aria-label="View">
              <button className={mode === "site" ? "on" : ""} onClick={() => setMode("site")} data-testid="view-site">
                Site 3D
              </button>
              <button className={mode === "hall" ? "on" : ""} onClick={() => setMode("hall")} data-testid="view-hall">
                Hall 3D
              </button>
              <button className={mode === "plan" ? "on" : ""} onClick={() => setMode("plan")} data-testid="toggle-view">
                Plan
              </button>
            </span>
            {title}
          </span>
          <span className="view-actions">
            {mode !== "site" && halls.length > 0 && (
              <select aria-label="Hall" value={hallId ?? ""} onChange={(e) => setRoom(e.target.value)}>
                {halls.map((h) => (
                  <option key={h.id} value={h.id}>
                    {h.id}
                  </option>
                ))}
              </select>
            )}
            {mode === "site" && selectedHall && (
              <button
                onClick={() => {
                  setRoom(selectedHall.id);
                  setMode("hall");
                }}
              >
                Open {selectedHall.id} in 3D
              </button>
            )}
            {three && (
              <select
                aria-label="3D quality"
                title={`3D quality: ${reason}`}
                value={quality}
                onChange={(e) => chooseQuality(e.target.value as Quality)}
                data-testid="quality"
              >
                <option value="high">High quality</option>
                <option value="low">Low GPU</option>
              </select>
            )}
          </span>
        </div>
        {mode === "site" && world && (
          <div className="site-controls">
            <select aria-label="Floor" value={floor ?? ""} onChange={(e) => setFloor(e.target.value || null)}>
              <option value="">All floors</option>
              {[...(world.site.floors ?? [])]
                .sort((a, b) => (a.index ?? 0) - (b.index ?? 0))
                .map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.id}
                  </option>
                ))}
            </select>
            <label>
              <input type="checkbox" checked={explode} onChange={(e) => setExplode(e.target.checked)} /> Explode floors
            </label>
            {LAYERS.map(([k, label]) => (
              <label key={k}>
                <input type="checkbox" checked={layers.has(k)} onChange={() => toggleLayer(k)} /> {label}
              </label>
            ))}
          </div>
        )}
        {mode === "site" && world && (
          <SceneBoundary>
            <Suspense fallback={null}>
              <SiteView
                world={world}
                scope={scope}
                points={points}
                options={siteOptions}
                quality={quality}
                onSlow={stepDown}
                onHover={setHover}
              />
            </Suspense>
            {hover && <p className="view-hover">{hover}</p>}
            <Hint />
          </SceneBoundary>
        )}
        {mode === "hall" && hall && (
          <SceneBoundary>
            <Suspense fallback={null}>
              <HallView world={hall} options={VIEW} quality={quality} onSlow={stepDown} />
            </Suspense>
            <Hint />
          </SceneBoundary>
        )}
        {mode === "plan" && hall && <Plan world={hall} />}
      </section>
      <Inspector sid={sid} world={world} points={points} />
      <div className="ops-bottom">
        <FaultPanel sid={sid} world={world} />
        <Propagation sid={sid} />
      </div>
    </div>
  );
}

function Hint() {
  return (
    <p className="view-hint" aria-hidden>
      Drag to orbit · right-drag or Shift-drag to pan · wheel to zoom · click an asset to fly to it, empty space to go
      back
    </p>
  );
}

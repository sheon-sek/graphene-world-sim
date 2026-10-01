import { lazy, Suspense, useMemo, useState } from "react";
import { useSession } from "../app/session";
import { hallOf, sessionRoom, useWorld } from "../live/world";
import type { ViewOptions } from "../scene/HallView";
import { initialQuality, saveQuality, type Quality } from "../scene/quality";
import { SceneBoundary } from "../scene/SceneBoundary";
import { AlarmsPanel } from "./Alarms";
import { FaultPanel } from "./FaultPanel";
import { Inspector } from "./Inspector";
import { Plan } from "./Plan";
import { Propagation } from "./Propagation";

// three.js loads only when the 3D view is shown.
const HallView = lazy(() => import("../scene/HallView").then((m) => ({ default: m.HallView })));

const VIEW: ViewOptions = { airField: true, ambientOcclusion: true, bloom: true };

function initial3d(): boolean {
  try {
    const q = new URLSearchParams(window.location.search).get("3d");
    if (q !== null) return q !== "0";
    return localStorage.getItem("gws.view") !== "plan";
  } catch {
    return true;
  }
}

/** Watch the hall, inspect assets, follow alarms, inject faults and trace their consequences. */
export function OperationsPage({ sid }: { sid: string }) {
  const revision = useSession((s) => s.info?.revision);
  // The session's description changes with every poll while it runs (its clock moves); the hall
  // depends only on its revision and scope, so it is rebuilt only when one of those changes.
  const scopeKey = useSession((s) => s.info?.scope.join("\n"));
  const { world, error } = useWorld(revision);
  const [three, setThree] = useState(initial3d);
  const [{ quality, reason }, setQuality] = useState(initialQuality);
  const chooseQuality = (q: Quality) => {
    saveQuality(q);
    setQuality({ quality: q, reason: "your choice" });
  };
  const hall = useMemo(() => {
    if (!world || scopeKey === undefined) return null;
    const scope = new Set(scopeKey.split("\n"));
    const room = new URLSearchParams(window.location.search).get("room") ?? sessionRoom(world, scope);
    return room ? hallOf(world, room, scope) : null;
  }, [world, scopeKey]);

  const toggle = () => {
    setThree((v) => {
      try {
        localStorage.setItem("gws.view", v ? "plan" : "3d");
      } catch {
        /* a private window keeps the choice for this page only */
      }
      return !v;
    });
  };

  return (
    <div className="ops">
      <AlarmsPanel sid={sid} />
      <section className="view" data-testid="hall-view">
        <div className="view-bar">
          <span>{hall ? hall.room.name : error ? `Could not load the hall: ${error}` : "Loading the hall…"}</span>
          <span className="view-actions">
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
            <button onClick={toggle} data-testid="toggle-view">
              {three ? "Plan view" : "3D view"}
            </button>
          </span>
        </div>
        {hall &&
          (three ? (
            <SceneBoundary>
              <Suspense fallback={null}>
                <HallView
                  world={hall}
                  options={VIEW}
                  quality={quality}
                  onSlow={(fps) => {
                    saveQuality("low");
                    setQuality({ quality: "low", reason: `stepped down at ${Math.round(fps)} fps` });
                  }}
                />
              </Suspense>
              <p className="view-hint" aria-hidden>
                Drag to orbit · right-drag or Shift-drag to pan · wheel to zoom · click an asset to fly to it, empty
                floor to go back
              </p>
            </SceneBoundary>
          ) : (
            <Plan world={hall} />
          ))}
      </section>
      <Inspector sid={sid} world={world} />
      <div className="ops-bottom">
        <FaultPanel sid={sid} world={world} />
        <Propagation sid={sid} />
      </div>
    </div>
  );
}

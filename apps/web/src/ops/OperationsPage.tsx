import { lazy, Suspense, useMemo, useState } from "react";
import { useSession } from "../app/session";
import { hallOf, sessionRoom, useWorld } from "../live/world";
import type { ViewOptions } from "../scene/HallView";
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
  const info = useSession((s) => s.info);
  const { world, error } = useWorld(info?.revision);
  const [three, setThree] = useState(initial3d);
  const hall = useMemo(() => {
    if (!world || !info) return null;
    const scope = new Set(info.scope);
    const room = new URLSearchParams(window.location.search).get("room") ?? sessionRoom(world, scope);
    return room ? hallOf(world, room, scope) : null;
  }, [world, info]);

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
          <button onClick={toggle} data-testid="toggle-view">
            {three ? "Plan view" : "3D view"}
          </button>
        </div>
        {hall &&
          (three ? (
            <SceneBoundary>
              <Suspense fallback={null}>
                <HallView world={hall} options={VIEW} />
              </Suspense>
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

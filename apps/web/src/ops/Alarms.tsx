import { useState } from "react";
import { api, must } from "../api/client";
import { clock, shortName } from "../app/format";
import { usePoll, useSession } from "../app/session";
import { useSim } from "../live/store";
import type { PointIndex, WorldData } from "../live/world";
import { AssetTree, PointBrowser } from "./SiteBrowser";

export interface AlarmEntry {
  id: number;
  point: string;
  asset: string | null;
  raised_t: number;
  cleared_t: number | null;
  active: boolean;
  context: SessionEvent | null;
}

export interface SessionEvent {
  step: number;
  t: number;
  kind: string;
  payload: Record<string, unknown>;
}

export function describe(e: SessionEvent): string {
  const p = e.payload;
  switch (e.kind) {
    case "fault":
      return `Fault ${String(p.mode)} on ${shortName(String(p.target))}`;
    case "clear":
      return `Cleared fault ${String(p.id)}`;
    case "reset":
      return `Reset ${shortName(String(p.target))}`;
    case "command":
      return `Command ${shortName(String(p.target))}${p.signal ? `.${String(p.signal)}` : ""} = ${String(p.value)}`;
    case "conditions":
      return `Conditions ${Object.keys((p.changes as object) ?? {}).join(", ")}`;
    case "reinit":
      return `Reinitialised on revision ${String(p.revision)}`;
    case "snapshot":
      return `Snapshot ${String(p.id)}${p.label ? ` “${String(p.label)}”` : ""}`;
    case "restore":
      return `Restored snapshot ${String(p.id)}`;
    case "run":
      return `Run at ${Number(p.speed) === 0 ? "max" : `${String(p.speed)}×`}`;
    default:
      return e.kind[0].toUpperCase() + e.kind.slice(1);
  }
}

/** The alarm log raised by the scope's fault-alarm points, and the session's event log (#52). */
export function AlarmsPanel({
  sid,
  world,
  points,
  scope,
}: {
  sid: string;
  world: WorldData | null;
  points: PointIndex | null;
  scope: Set<string>;
}) {
  const [tab, setTab] = useState<"alarms" | "events" | "assets" | "points">("alarms");
  const [alarms, setAlarms] = useState<AlarmEntry[]>([]);
  const [events, setEvents] = useState<SessionEvent[]>([]);
  const version = useSession((s) => s.version);
  const select = useSim((s) => s.select);
  usePoll(
    async () => {
      const [a, e] = await Promise.all([
        must(api.GET("/api/runtime/sessions/{sid}/alarms", { params: { path: { sid } } })).catch(() => null),
        must(api.GET("/api/runtime/sessions/{sid}/events", { params: { path: { sid } } })).catch(() => null),
      ]);
      if (a) setAlarms(a as unknown as AlarmEntry[]);
      if (e) setEvents((e as unknown as SessionEvent[]).slice().reverse());
    },
    1500,
    [sid, version],
  );
  const active = alarms.filter((a) => a.active);
  return (
    <section className="panel alarms-panel" data-testid="alarms-panel">
      <nav className="subtabs">
        <button className={tab === "alarms" ? "on" : ""} onClick={() => setTab("alarms")}>
          Alarms {active.length > 0 && <span className="count alarm" data-testid="active-alarms">{active.length}</span>}
        </button>
        <button className={tab === "events" ? "on" : ""} onClick={() => setTab("events")}>
          Events
        </button>
        <button className={tab === "assets" ? "on" : ""} onClick={() => setTab("assets")} data-testid="tab-assets">
          Assets
        </button>
        <button className={tab === "points" ? "on" : ""} onClick={() => setTab("points")} data-testid="tab-points">
          Points
        </button>
      </nav>
      {tab === "assets" ? (
        <AssetTree world={world} scope={scope} />
      ) : tab === "points" ? (
        <PointBrowser points={points} />
      ) : tab === "alarms" ? (
        <ul className="log">
          {alarms.length === 0 && <li className="muted">No alarms.</li>}
          {alarms.map((a) => (
            <li key={a.id} className={a.active ? "active" : "cleared"} data-testid="alarm">
              <button onClick={() => a.asset && select(a.asset)}>
                <span className={`dot ${a.active ? "alarm" : ""}`} />
                <b>{a.asset ? shortName(a.asset) : ""}</b> {a.point.split("/").pop()}
                <small>
                  {clock(a.raised_t)}
                  {a.cleared_t !== null ? ` to ${clock(a.cleared_t)}` : " · active"}
                  {a.context ? ` · after ${describe(a.context).toLowerCase()}` : ""}
                </small>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <ul className="log">
          {events.map((e, i) => (
            <li key={`${e.step}:${i}`} className={`event ${e.kind}`}>
              <span className="time">{clock(e.t)}</span> {describe(e)}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

import { useState } from "react";
import { api, must } from "../api/client";
import { clock, shortName } from "../app/format";
import { navigate } from "../app/router";
import { usePoll, useSession } from "../app/session";
import type { SessionEvent } from "./Alarms";

interface Partition {
  name: string;
  assets: string[];
  rooms: string[];
  compile_seconds: number | null;
  solver_steps: number | null;
  events: number | null;
  step_seconds: number | null;
}

interface Diagnostics {
  t: number;
  step: number;
  dt: number;
  running: boolean;
  speed: number;
  real_time_factor: number | null;
  partitions: Partition[];
  timings: { steps: number; last: Record<string, number>; mean: Record<string, number>; slowest: number };
  not_modelled: Record<string, string>;
  missing_blocks: string[];
  history: { frames: number; span: [number, number] | null };
  subscribers: number;
  last_error: { during: string; type: string; message: string } | null;
}

const ms = (s: number | null | undefined) => (s == null ? "–" : `${(s * 1000).toFixed(s < 0.01 ? 2 : 1)} ms`);

/** How the session runs: solver work, step timing, what the scope leaves out, errors, and
 * snapshots to go back to (#56). */
export function DiagnosticsPage({ sid }: { sid: string }) {
  const version = useSession((s) => s.version);
  const act = useSession((s) => s.act);
  const [d, setD] = useState<Diagnostics | null>(null);
  const [snapshots, setSnapshots] = useState<SessionEvent[]>([]);
  const [label, setLabel] = useState("");
  usePoll(
    async () => {
      const [diag, events] = await Promise.all([
        must(api.GET("/api/runtime/sessions/{sid}/diagnostics", { params: { path: { sid } } })).catch(() => null),
        must(api.GET("/api/runtime/sessions/{sid}/events", { params: { path: { sid } } })).catch(() => null),
      ]);
      if (diag) setD(diag as unknown as Diagnostics);
      if (events) setSnapshots((events as unknown as SessionEvent[]).filter((e) => e.kind === "snapshot"));
    },
    2000,
    [sid, version],
  );
  if (!d) return <div className="diagnostics muted">Loading diagnostics…</div>;
  const phases = Object.entries(d.timings.mean).filter(([k]) => k !== "total");
  const total = d.timings.mean.total ?? 0;
  const path = { params: { path: { sid } } };

  return (
    <div className="diagnostics" data-testid="diagnostics">
      <section className="panel">
        <h3>Run</h3>
        <div className="kpis">
          <div>
            <span>Simulated</span>
            <b>{clock(d.t)}</b>
          </div>
          <div>
            <span>Steps</span>
            <b>{d.step}</b>
          </div>
          <div>
            <span>Step</span>
            <b>{d.dt} s</b>
          </div>
          <div>
            <span>Real-time factor</span>
            <b data-testid="rtf">{d.real_time_factor ? `${Math.round(d.real_time_factor)}×` : "–"}</b>
          </div>
          <div>
            <span>Mean step</span>
            <b>{ms(total)}</b>
          </div>
          <div>
            <span>Slowest step</span>
            <b>{ms(d.timings.slowest)}</b>
          </div>
        </div>
        <div className="phase-bar" title="Mean wall time per step, by phase">
          {phases.map(([k, v]) => (
            <span key={k} style={{ flexGrow: total ? v / total : 0 }} title={`${k}: ${ms(v)}`}>
              {total && v / total > 0.12 ? k : ""}
            </span>
          ))}
        </div>
      </section>
      {d.last_error && (
        <section className="panel error-panel" role="alert">
          <h3>
            Last error, during {d.last_error.during}: {d.last_error.type}
          </h3>
          <pre>{d.last_error.message}</pre>
        </section>
      )}
      <section className="panel">
        <h3>Partitions</h3>
        {d.partitions.length === 0 ? (
          <p className="muted">No thermofluid partition: nothing in scope has a thermofluid model.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>Partition</th>
                <th>Assets</th>
                <th>Rooms</th>
                <th>Compile</th>
                <th>Solver steps</th>
                <th>Events</th>
                <th>Per step</th>
              </tr>
            </thead>
            <tbody>
              {d.partitions.map((p) => (
                <tr key={p.name}>
                  <td>
                    <code>{p.name}</code>
                  </td>
                  <td title={p.assets.join("\n")}>{p.assets.length}</td>
                  <td>{p.rooms.join(", ")}</td>
                  <td>{p.compile_seconds ? `${p.compile_seconds.toFixed(0)} s` : "cached"}</td>
                  <td>{p.solver_steps ?? "–"}</td>
                  <td>{p.events ?? "–"}</td>
                  <td>{ms(p.step_seconds)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
      <section className="panel">
        <h3>Not modelled</h3>
        <ul className="log">
          {Object.entries(d.not_modelled).map(([a, why]) => (
            <li key={a}>
              <b>{shortName(a)}</b> <small>{why}</small>
            </li>
          ))}
          {d.missing_blocks.map((b) => (
            <li key={b}>
              <b>{b}</b> <small>controller not built: an input or output is out of scope</small>
            </li>
          ))}
          {Object.keys(d.not_modelled).length + d.missing_blocks.length === 0 && <li className="muted">Everything in scope is modelled.</li>}
        </ul>
      </section>
      <section className="panel">
        <h3>Snapshots</h3>
        <div className="inline-form">
          <input placeholder="Label" value={label} onChange={(e) => setLabel(e.target.value)} />
          <button
            onClick={() =>
              void act(null, () => must(api.POST("/api/runtime/sessions/{sid}/snapshots", { ...path, body: { label } }))).then(() =>
                setLabel(""),
              )
            }
          >
            Take snapshot
          </button>
          <button
            title="A new session that re-runs this one's events from the start"
            onClick={() =>
              void act("Replaying the session's events…", () => must(api.POST("/api/runtime/sessions/{sid}/replay", path))).then(
                (copy) => copy && navigate({ name: "session", session: copy.id, workspace: "operations" }),
              )
            }
          >
            Replay as new session
          </button>
        </div>
        <ul className="log">
          {snapshots.map((s) => (
            <li key={String(s.payload.id)}>
              <span className="time">{clock(s.t)}</span> {String(s.payload.id)} {s.payload.label ? `“${String(s.payload.label)}”` : ""}
              <span className="actions">
                <button
                  disabled={d.running}
                  onClick={() =>
                    void act(null, () =>
                      must(
                        api.POST("/api/runtime/sessions/{sid}/snapshots/{snap_id}/restore", {
                          params: { path: { sid, snap_id: String(s.payload.id) } },
                        }),
                      ),
                    )
                  }
                >
                  Restore
                </button>
              </span>
            </li>
          ))}
        </ul>
        <p className="muted">
          History keeps {d.history.frames} frames
          {d.history.span ? `, ${clock(d.history.span[0])} to ${clock(d.history.span[1])}` : ""}. {d.subscribers} stream
          subscriber{d.subscribers === 1 ? "" : "s"}.
        </p>
      </section>
    </div>
  );
}

import { useState } from "react";
import { api, must } from "../api/client";
import { clock, formatValue, shortName } from "../app/format";
import { usePoll, useSession } from "../app/session";
import { useSim } from "../live/store";
import { describe, type SessionEvent } from "./Alarms";

interface Change {
  signal: string;
  unit: string | null;
  t: number;
  before: number;
  at: number;
  after: number;
}

interface Reached {
  asset: string;
  t: number;
  changes: Change[];
}

const CAUSES = new Set(["fault", "clear", "reset", "command", "conditions"]);

/**
 * Where the consequences of a cause went, and when: every asset whose simulated state moved
 * after the cause, in the order it moved (#57). The runtime computes the consequences; this
 * only lines them up.
 */
export function Propagation({ sid }: { sid: string }) {
  const version = useSession((s) => s.version);
  const now = useSim((s) => s.frame?.t ?? 0);
  const select = useSim((s) => s.select);
  const [causes, setCauses] = useState<SessionEvent[]>([]);
  const [chosen, setChosen] = useState<number | null>(null);
  const [reached, setReached] = useState<Reached[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const cause = causes.find((c) => c.step === chosen) ?? causes[0];

  usePoll(
    async () => {
      const events = (await must(api.GET("/api/runtime/sessions/{sid}/events", { params: { path: { sid } } })).catch(
        () => [],
      )) as unknown as SessionEvent[];
      const found = events.filter((e) => CAUSES.has(e.kind)).reverse();
      setCauses(found);
      const c = found.find((x) => x.step === chosen) ?? found[0];
      if (!c) return setReached([]);
      const next = found.find((x) => x.t > c.t);
      const data = await must(
        api.GET("/api/runtime/sessions/{sid}/propagation", {
          params: { path: { sid }, query: { since: c.t, until: next ? next.t : undefined } },
        }),
      ).catch(() => null);
      if (data) setReached(data as unknown as Reached[]);
    },
    2500,
    [sid, version, chosen],
  );

  const t0 = cause?.t ?? 0;
  const span = Math.max(60, ...reached.map((r) => r.t - t0), Math.min(now - t0, 3600));
  return (
    <section className="panel propagation" data-testid="propagation">
      <header className="panel-head">
        <h3>Propagation</h3>
        <select
          aria-label="Cause"
          value={cause?.step ?? ""}
          onChange={(e) => setChosen(Number(e.target.value))}
          disabled={causes.length === 0}
        >
          {causes.map((c) => (
            <option key={`${c.step}:${c.kind}`} value={c.step}>
              {clock(c.t)} · {describe(c)}
            </option>
          ))}
        </select>
      </header>
      {!cause ? (
        <p className="muted">Inject a fault or change a condition to see where its consequences go.</p>
      ) : reached.length === 0 ? (
        <p className="muted">Nothing has moved beyond its threshold yet.</p>
      ) : (
        <ol className="timeline">
          {reached.map((r) => (
            <li key={r.asset} data-testid="reached">
              <button className="lane" onClick={() => setOpen(open === r.asset ? null : r.asset)}>
                <span className="who" title={r.asset}>
                  {r.asset.startsWith("room:") ? `Room ${r.asset.slice(5)}` : shortName(r.asset)}
                </span>
                <span className="track">
                  <span className="bar" style={{ left: `${((r.t - t0) / span) * 100}%` }} />
                </span>
                <span className="when">+{Math.round(r.t - t0)} s</span>
              </button>
              {open === r.asset && (
                <table className="changes">
                  <tbody>
                    {r.changes.map((c) => (
                      <tr key={c.signal}>
                        <td>{c.signal}</td>
                        <td>+{Math.round(c.t - t0)} s</td>
                        <td>
                          {formatValue(c.signal, c.before, c.unit)} → {formatValue(c.signal, c.after, c.unit)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr>
                      <td colSpan={3}>
                        {!r.asset.startsWith("room:") && (
                          <button className="link" onClick={() => select(r.asset)}>
                            Inspect {shortName(r.asset)}
                          </button>
                        )}
                      </td>
                    </tr>
                  </tfoot>
                </table>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

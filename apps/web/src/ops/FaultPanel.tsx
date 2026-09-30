import { useEffect, useMemo, useState } from "react";
import { api, must } from "../api/client";
import { shortName } from "../app/format";
import { useSession } from "../app/session";
import type { ActiveFault } from "../live/frames";
import { useSim } from "../live/store";
import type { WorldData } from "../live/world";

const NO_FAULTS: never[] = [];

/** Inject a fault mode on an asset in scope, and clear or reset the active ones (#55). */
export function FaultPanel({ sid, world }: { sid: string; world: WorldData | null }) {
  const scope = useSession((s) => s.info?.scope);
  const act = useSession((s) => s.act);
  const selected = useSim((s) => s.selected);
  // The session's own list: it changes as soon as a fault is injected, cleared or reset, while
  // the frames show it only from the next step.
  const faults = (useSession((s) => s.info?.faults) ?? NO_FAULTS) as unknown as ActiveFault[];
  const targets = useMemo(
    () =>
      (scope ?? []).filter((id) => {
        const a = world?.assets.get(id);
        return a && Object.keys(world?.types.get(a.type)?.fault_modes ?? {}).length > 0;
      }),
    [scope, world],
  );
  const [target, setTarget] = useState("");
  const [mode, setMode] = useState("");
  const [severity, setSeverity] = useState(1);
  const [ramp, setRamp] = useState(0);
  const [duration, setDuration] = useState("");
  const [params, setParams] = useState<Record<string, string>>({});

  useEffect(() => {
    if (selected && targets.includes(selected)) setTarget(selected);
  }, [selected, targets]);
  const current = target || targets[0] || "";
  const type = world?.types.get(world?.assets.get(current)?.type ?? "");
  const modes = Object.entries(type?.fault_modes ?? {});
  const chosen = modes.find(([m]) => m === mode) ?? modes[0];

  async function inject() {
    if (!chosen) return;
    const parameters = Object.fromEntries(
      Object.entries(params)
        .filter(([k, v]) => v !== "" && k in (chosen[1].parameters ?? {}))
        .map(([k, v]) => [k, Number(v)]),
    );
    await act(null, () =>
      must(
        api.POST("/api/runtime/sessions/{sid}/faults", {
          params: { path: { sid } },
          body: {
            target: current,
            mode: chosen[0],
            parameters,
            severity,
            ramp_s: ramp,
            duration_s: duration ? Number(duration) : null,
          },
        }),
      ),
    );
  }

  const clear = (id: string) =>
    act(null, () => must(api.DELETE("/api/runtime/sessions/{sid}/faults/{fault_id}", { params: { path: { sid, fault_id: id } } })));
  const reset = (t: string) =>
    act(null, () => must(api.POST("/api/runtime/sessions/{sid}/reset", { params: { path: { sid } }, body: { target: t } })));

  return (
    <section className="panel fault-panel" data-testid="fault-panel">
      <h3>Faults</h3>
      <div className="form-grid">
        <label>
          Asset
          <select value={current} onChange={(e) => setTarget(e.target.value)} data-testid="fault-target">
            {targets.map((id) => (
              <option key={id} value={id}>
                {shortName(id)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Mode
          <select value={chosen?.[0] ?? ""} onChange={(e) => setMode(e.target.value)} data-testid="fault-mode">
            {modes.map(([m]) => (
              <option key={m} value={m}>
                {m.replace(/_/g, " ")}
              </option>
            ))}
          </select>
        </label>
        <label>
          Severity {Math.round(severity * 100)} %
          <input type="range" min={0} max={1} step={0.05} value={severity} onChange={(e) => setSeverity(Number(e.target.value))} />
        </label>
        <label>
          Ramp (s)
          <input type="number" min={0} value={ramp} onChange={(e) => setRamp(Number(e.target.value))} />
        </label>
        <label>
          Duration (s)
          <input type="number" min={0} placeholder="until cleared" value={duration} onChange={(e) => setDuration(e.target.value)} />
        </label>
        {Object.entries(chosen?.[1].parameters ?? {}).map(([k, spec]) => (
          <label key={k}>
            {k} {spec.unit ? `(${spec.unit})` : ""}
            <input
              type="number"
              placeholder={spec.default != null ? String(spec.default) : ""}
              value={params[k] ?? ""}
              onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.value }))}
            />
          </label>
        ))}
      </div>
      {chosen?.[1].description && <p className="muted">{chosen[1].description}</p>}
      <button className="danger" disabled={!chosen} onClick={() => void inject()} data-testid="inject">
        Inject {chosen ? chosen[0].replace(/_/g, " ") : "fault"} on {shortName(current)}
      </button>
      <ul className="log active-faults">
        {faults.map((f) => (
          <li key={f.id} data-testid="active-fault">
            <span className="dot alarm" />
            <b>{f.id}</b> {f.mode} on {shortName(f.target)}
            {f.cleared_at != null && <small> · cause cleared, latched until reset</small>}
            <span className="actions">
              {f.cleared_at == null && (
                <button onClick={() => void clear(f.id)} data-testid={`clear-${f.id}`}>
                  Clear
                </button>
              )}
              <button onClick={() => void reset(f.target)} data-testid={`reset-${f.id}`}>
                Reset
              </button>
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

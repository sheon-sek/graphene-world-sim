import { useMemo, useState } from "react";
import { api, must } from "../api/client";
import { display, formatValue, guessUnit, shortName } from "../app/format";
import { usePoll, useSession } from "../app/session";
import type { Frame } from "../live/frames";
import { useSim } from "../live/store";
import type { PointIndex, WorldData } from "../live/world";
import { Trend, type TrendSeries } from "./Trend";

const NO_STATE: Frame["state"][string] = {};
const DEFAULT_SIGNALS = ["TSupAir", "TRetAir", "TAir", "mAir_flow", "Q", "P", "TChwLvg", "m_flow"];

function defaults(signals: string[]): string[] {
  const picked = DEFAULT_SIGNALS.filter((s) => signals.includes(s)).slice(0, 3);
  return picked.length ? picked : signals.filter((s) => guessUnit(s) === "K").slice(0, 2);
}

function toTrend(values: (number | null)[], signal: string): TrendSeries {
  const unit = display(signal, 0).unit;
  const scale = (v: number) => Number(display(signal, v).value);
  return { label: signal, unit, values: unit ? values.map((v) => (v === null ? null : scale(v))) : values };
}

/** The selected asset: its live state and points, and trends of the signals picked (#51). */
export function Inspector({ sid, world, points: index }: { sid: string; world: WorldData | null; points?: PointIndex | null }) {
  const selected = useSim((s) => s.selected);
  const select = useSim((s) => s.select);
  const state = useSim((s) => (selected ? (s.frame?.state[selected] ?? NO_STATE) : NO_STATE));
  const points = useSim((s) => s.frame?.points);
  const faults = useSim((s) => s.frame?.faults);
  const version = useSession((s) => s.version);
  const scope = useSession((s) => s.info?.scope);
  const signals = useMemo(
    () => Object.keys(state).filter((k) => typeof state[k] === "number" || typeof state[k] === "boolean"),
    [state],
  );
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const chosen = selected ? (picked[selected] ?? defaults(signals)) : [];
  const [trend, setTrend] = useState<{ t: number[]; series: TrendSeries[] } | null>(null);
  const key = `${selected}|${chosen.join(",")}`;

  usePoll(
    async () => {
      if (!selected || chosen.length === 0) return setTrend(null);
      const data = await must(
        api.POST("/api/runtime/sessions/{sid}/history", {
          params: { path: { sid } },
          body: { series: chosen.map((signal) => ({ asset: selected, signal })), max_points: 600 },
        }),
      ).catch(() => null);
      if (!data) return;
      const d = data as { t: number[]; series: { values: (number | null)[] | null }[] };
      setTrend({ t: d.t, series: d.series.map((s, i) => toTrend(s.values ?? [], chosen[i])) });
    },
    2000,
    [key, version],
  );

  if (!selected) return <aside className="panel inspector-panel empty">Select an asset to inspect it.</aside>;
  const asset = world?.assets.get(selected);
  const type = asset ? world?.types.get(asset.type) : undefined;
  const inScope = scope?.includes(selected) ?? false;
  // Every point the asset owns: under its path, or reading it from elsewhere (plant views).
  const ownPaths = index?.byAsset.get(selected) ?? Object.keys(points ?? {}).filter((p) => p.startsWith(`${selected}/`));
  const own = ownPaths.map((p) => [p, points?.[p]] as const);
  const active = (faults ?? []).filter((f) => f.target === selected);
  const toggle = (signal: string) =>
    setPicked((p) => {
      const now = p[selected] ?? defaults(signals);
      return { ...p, [selected]: now.includes(signal) ? now.filter((s) => s !== signal) : [...now, signal].slice(-4) };
    });

  return (
    <aside className="panel inspector-panel" data-testid="inspector">
      <header>
        <div>
          <span className="eyebrow">{type?.name ?? asset?.type ?? "Asset"}</span>
          <h2>{asset?.name ?? shortName(selected)}</h2>
          <code>{selected}</code>
        </div>
        <button className="icon" aria-label="Close" onClick={() => select(null)}>
          ×
        </button>
      </header>
      {!inScope && <p className="note">Not in this session's scope: it is not simulated.</p>}
      {active.map((f) => (
        <p key={f.id} className="note alarm-note">
          Fault {f.id}: {f.mode}
        </p>
      ))}
      {trend && trend.series.length > 0 && <Trend t={trend.t} series={trend.series} />}
      {signals.length > 0 && (
        <>
          <h3>State</h3>
          <div className="rows" data-testid="state-rows">
            {signals.map((s) => (
              <button key={s} className={`row ${chosen.includes(s) ? "on" : ""}`} onClick={() => toggle(s)} title="Trend this signal">
                <span>{s}</span>
                <b data-signal={s}>{formatValue(s, state[s])}</b>
              </button>
            ))}
          </div>
        </>
      )}
      {own.length > 0 && (
        <>
          <h3>
            Points <small className="muted">{own.length}</small>
          </h3>
          <div className="rows points" data-testid="inspector-points">
            {own.map(([p, v]) => (
              <div key={p} className={`row quality-${v?.quality ?? "none"}`} title={`${p}${v?.reason ? ` · ${v.reason}` : ""}`}>
                <span>{p.startsWith(`${selected}/`) ? p.slice(selected.length + 1) : p}</span>
                <b>
                  {v === undefined || v.value === null
                    ? "–"
                    : typeof v.value === "number"
                      ? v.value.toFixed(2)
                      : String(v.value)}
                  {index?.bindings.get(p)?.unit ? ` ${index.bindings.get(p)?.unit}` : ""}
                </b>
              </div>
            ))}
          </div>
        </>
      )}
    </aside>
  );
}

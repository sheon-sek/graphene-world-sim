import { useEffect, useMemo, useState } from "react";
import { kelvinToCelsius, ReplaySource, stateNumber, type Frame } from "../live/frames";
import { loadLiveHall, loadRecording, type LoadedHall } from "../live/hall";
import { useSim } from "../live/store";
import { frameStats, HallView, type ViewOptions } from "../scene/HallView";
import { layoutHall, type HallLayout, type SceneAsset } from "../scene/hall/layout";
import { DESIGN_FAN_RPM, FAN_SLOW_MOTION } from "../scene/hall/units";
import { rendererInfo } from "../scene/renderer";
import { SceneBoundary } from "../scene/SceneBoundary";
import { temperatureColour } from "../scene/thermal";
import "./hero.css";

const DEFAULT_RECORDING = "fixtures/dh01-fcu-trip.json";

function clock(t: number): string {
  const s = Math.max(0, Math.round(t));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h > 0 ? `${h}:` : ""}${String(m).padStart(h > 0 ? 2 : 1, "0")}:${String(sec).padStart(2, "0")}`;
}

function css(celsius: number): string {
  const [r, g, b] = temperatureColour(celsius);
  return `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
}

function useLoadedHall(): { hall: LoadedHall | null; error: string | null } {
  const [hall, setHall] = useState<LoadedHall | null>(null);
  const [error, setError] = useState<string | null>(null);
  const connect = useSim((s) => s.connect);
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const api = q.get("api");
    const session = q.get("session");
    const load =
      api && session
        ? loadLiveHall(api, session, q.get("room") ?? "DH01")
        : loadRecording(q.get("recording") ?? DEFAULT_RECORDING, Number(q.get("speed") ?? 10));
    let cancelled = false;
    load
      .then((h) => {
        if (cancelled) return;
        connect(h.source);
        setHall(h);
        const selected = q.get("select");
        if (selected) window.setTimeout(() => useSim.getState().select(selected), 300);
      })
      .catch((e: unknown) => setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, [connect]);
  return { hall, error };
}

function Metric({ label, value, unit, tone }: { label: string; value: string; unit: string; tone?: string }) {
  return (
    <div className="metric">
      <span className="metric-label">{label}</span>
      <span className="metric-value" style={tone ? { color: tone } : undefined}>
        {value}
        <small>{unit}</small>
      </span>
    </div>
  );
}

function hallTotals(frame: Frame | null, layout: HallLayout, room: string) {
  let cooling = 0;
  let chw = 0;
  let chwUnits = 0;
  for (const a of layout.assets) {
    if (!a.in_scope || (a.kind !== "fan-coil" && a.kind !== "ceiling-coils")) continue;
    cooling += stateNumber(frame, a.id, "Q");
    const t = stateNumber(frame, a.id, "TChwEnt", NaN);
    if (Number.isFinite(t)) {
      chw += t;
      chwUnits += 1;
    }
  }
  const it = layout.assets.find((a) => a.kind === "it-load" && a.in_scope);
  return {
    room: kelvinToCelsius(stateNumber(frame, `room:${room}`, "TAir", NaN)),
    it: it ? stateNumber(frame, it.id, "P") / 1000 : NaN,
    cooling: cooling / 1000,
    chw: chwUnits ? kelvinToCelsius(chw / chwUnits) : NaN,
  };
}

function Readout({ layout, room }: { layout: HallLayout; room: string }) {
  const frame = useSim((s) => s.frame);
  const totals = hallTotals(frame, layout, room);
  const fixed = (v: number, d = 1) => (Number.isFinite(v) ? v.toFixed(d) : "–");
  return (
    <div className="metrics">
      <Metric label="Hall air" value={fixed(totals.room)} unit="°C" tone={css(totals.room)} />
      <Metric label="IT load" value={fixed(totals.it, 0)} unit="kW" />
      <Metric label="Cooling" value={fixed(totals.cooling, 0)} unit="kW" />
      <Metric label="CHW supply" value={fixed(totals.chw)} unit="°C" tone={css(totals.chw)} />
    </div>
  );
}

const NO_FAULTS: Frame["faults"] = [];

function Alarms() {
  const faults = useSim((s) => s.frame?.faults ?? NO_FAULTS);
  const select = useSim((s) => s.select);
  if (faults.length === 0) return null;
  return (
    <div className="alarms" role="alert">
      {faults.map((f) => (
        <button key={f.id} className="alarm" onClick={() => select(f.target)}>
          <span className="alarm-dot" />
          <b>{f.target}</b>
          <span>{f.mode}</span>
        </button>
      ))}
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="row">
      <span>{k}</span>
      <b>{v}</b>
    </div>
  );
}

function unitRows(frame: Frame | null, a: SceneAsset): [string, string][] {
  const n = (k: string, f = NaN) => stateNumber(frame, a.id, k, f);
  const c = (k: string) => {
    const v = n(k);
    return Number.isFinite(v) ? `${kelvinToCelsius(v).toFixed(1)} °C` : "–";
  };
  const tripped = Boolean(frame?.state[a.id]?.tripped);
  const design = a.parameters?.m_air_flow_nominal ?? NaN;
  const flow = n("mAir_flow", 0);
  const fraction = Number.isFinite(design) && design > 0 ? flow / design : NaN;
  return [
    ["Status", tripped ? "Tripped" : frame?.state[a.id]?.energised ? "Running" : "Off"],
    ["Air flow", `${flow.toFixed(1)} kg/s (${Number.isFinite(fraction) ? Math.round(fraction * 100) : "–"} % of design)`],
    ["Fan speed", Number.isFinite(fraction) && !tripped ? `${Math.round(DESIGN_FAN_RPM * fraction)} rpm` : "0 rpm"],
    ["Supply air", c("TSupAir")],
    ["Return air", c("TRetAir")],
    ["CHW in / out", `${c("TChwEnt")} / ${c("TChwLvg")}`],
    ["CHW flow", `${n("mChw_flow", 0).toFixed(1)} kg/s`],
    ["Cooling", `${(n("Q", 0) / 1000).toFixed(0)} kW`],
    ["Fan power", `${(n("PFan", 0) / 1000).toFixed(1)} kW`],
  ];
}

function Inspector({ layout }: { layout: HallLayout }) {
  const selected = useSim((s) => s.selected);
  const frame = useSim((s) => s.frame);
  const select = useSim((s) => s.select);
  const asset = layout.assets.find((a) => a.id === selected);
  if (!asset) return null;
  let rows: [string, string][] = [];
  if (!asset.in_scope) rows = [["Simulation", "Not in this session's scope"]];
  else if (asset.kind === "fan-coil" || asset.kind === "ceiling-coils" || asset.kind === "perimeter")
    rows = unitRows(frame, asset);
  else if (asset.kind === "it-load")
    rows = [
      ["Power", `${(stateNumber(frame, asset.id, "P") / 1000).toFixed(0)} kW`],
      ["Racks", `${layout.rackCount}`],
      ["Supply", frame?.state[asset.id]?.energised ? "Energised" : "Not energised"],
    ];
  else if (asset.kind === "cooling-block")
    rows = [
      ["CHW flow", `${stateNumber(frame, asset.id, "m_flow").toFixed(1)} kg/s`],
      ["CHW in", `${kelvinToCelsius(stateNumber(frame, asset.id, "TEnt", NaN)).toFixed(1)} °C`],
      ["Pressure drop", `${(stateNumber(frame, asset.id, "dp") / 1000).toFixed(1)} kPa`],
    ];
  const points = Object.entries(frame?.points ?? {}).filter(([p]) => p.includes(asset.name));
  return (
    <aside className="inspector">
      <header>
        <div>
          <span className="eyebrow">{asset.type}</span>
          <h2>{asset.name}</h2>
          <code>{asset.id}</code>
        </div>
        <button className="icon" aria-label="Close" onClick={() => select(null)}>
          ×
        </button>
      </header>
      <div className="rows">
        {rows.map(([k, v]) => (
          <Row key={k} k={k} v={v} />
        ))}
      </div>
      {points.length > 0 && (
        <>
          <h3>Points</h3>
          <div className="rows points">
            {points.slice(0, 12).map(([p, v]) => (
              <Row
                key={p}
                k={p.slice(p.indexOf(asset.name) + asset.name.length + 1) || p}
                v={typeof v.value === "number" ? v.value.toFixed(2) : String(v.value)}
              />
            ))}
          </div>
        </>
      )}
      {(asset.kind === "fan-coil" || asset.kind === "ceiling-coils") && asset.in_scope && (
        <p className="note">
          Fan speed is the simulated air flow at an assumed {DESIGN_FAN_RPM} rpm design speed; the fans turn{" "}
          {FAN_SLOW_MOTION} times slower on screen so the blades stay readable.
        </p>
      )}
    </aside>
  );
}

function Playback() {
  const source = useSim((s) => s.source);
  const frame = useSim((s) => s.frame);
  const [, force] = useState(0);
  if (!source) return null;
  if (!(source instanceof ReplaySource)) {
    return (
      <div className="playback">
        <span className="chip live">Live</span>
        <span className="time">{clock(frame?.t ?? 0)}</span>
      </div>
    );
  }
  const events = source.frames.filter((f) => f.events && f.events.length > 0);
  const span = source.end_t - source.start_t;
  return (
    <div className="playback">
      <button
        className="icon"
        aria-label={source.playing ? "Pause" : "Play"}
        onClick={() => {
          source.playing = !source.playing;
          force((n) => n + 1);
        }}
      >
        {source.playing ? "❚❚" : "▶"}
      </button>
      <span className="time">{clock(frame?.t ?? 0)}</span>
      <div className="scrub">
        <input
          type="range"
          min={source.start_t}
          max={source.end_t}
          step={source.frames.length > 1 ? source.frames[1].t - source.frames[0].t : 1}
          value={frame?.t ?? 0}
          onChange={(e) => source.seek(Number(e.target.value))}
          aria-label="Simulation time"
        />
        {events.map((f) => (
          <span
            key={f.t}
            className={`mark ${f.events?.some((e) => e.kind === "fault") ? "fault" : "clear"}`}
            style={{ left: `${((f.t - source.start_t) / span) * 100}%` }}
            title={f.events?.map((e) => `${e.kind} ${e.target ?? ""}`).join(", ")}
          />
        ))}
      </div>
      <select
        value={source.speed}
        aria-label="Speed"
        onChange={(e) => {
          source.speed = Number(e.target.value);
          force((n) => n + 1);
        }}
      >
        {[1, 5, 10, 30, 60].map((s) => (
          <option key={s} value={s}>
            {s}×
          </option>
        ))}
      </select>
    </div>
  );
}

function Performance() {
  const [fps, setFps] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setFps(frameStats.fps), 500);
    return () => window.clearInterval(id);
  }, []);
  return (
    <div className="perf">
      <span className="chip">{rendererInfo.backend}</span>
      <span className="chip" data-testid="fps">
        {fps.toFixed(0)} fps
      </span>
    </div>
  );
}

function Views({ layout }: { layout: HallLayout }) {
  const select = useSim((s) => s.select);
  const selected = useSim((s) => s.selected);
  const units = layout.assets.filter((a) => a.in_scope && a.kind !== "sensor");
  return (
    <div className="views">
      <button className={selected === null ? "on" : ""} onClick={() => select(null)}>
        Overview
      </button>
      {units.map((a) => (
        <button key={a.id} className={selected === a.id ? "on" : ""} onClick={() => select(a.id)}>
          {a.kind === "it-load" ? "Racks" : a.name}
        </button>
      ))}
    </div>
  );
}

function Legend() {
  const stops = [16, 20, 24, 28, 32, 36];
  return (
    <div className="legend">
      <div className="bar" style={{ background: `linear-gradient(90deg, ${stops.map(css).join(",")})` }} />
      <div className="ticks">
        {stops.map((s) => (
          <span key={s}>{s}°</span>
        ))}
      </div>
    </div>
  );
}

export function HeroPage() {
  const { hall, error } = useLoadedHall();
  const [options, setOptions] = useState<ViewOptions>(() => {
    const q = new URLSearchParams(window.location.search);
    return { airField: q.get("field") !== "0", ambientOcclusion: q.get("ao") !== "0", bloom: q.get("bloom") !== "0" };
  });
  const layout = useMemo(() => (hall ? layoutHall(hall.world) : null), [hall]);
  if (error) return <div className="hero-error">Could not load the hall: {error}</div>;
  if (!hall || !layout) return <div className="hero-loading">Loading the hall…</div>;
  const { room, floor } = hall.world;
  const toggle = (k: keyof ViewOptions) => setOptions((o) => ({ ...o, [k]: !o[k] }));
  return (
    <div className="hero">
      <SceneBoundary>
        <HallView world={hall.world} options={options} />
      </SceneBoundary>
      <div className="hud top-left">
        <span className="eyebrow">
          {floor.id} · {room.w} × {room.h} m · {layout.rackCount} racks
        </span>
        <h1>{room.name}</h1>
        <p className="scenario">{hall.title}</p>
        <Readout layout={layout} room={room.id} />
        <Alarms />
      </div>
      <div className="hud top-right">
        <Performance />
        <div className="toggles">
          {(
            [
              ["airField", "Air temperature"],
              ["ambientOcclusion", "Ambient occlusion"],
              ["bloom", "Bloom"],
            ] as [keyof ViewOptions, string][]
          ).map(([k, label]) => (
            <label key={k}>
              <input type="checkbox" checked={options[k]} onChange={() => toggle(k)} />
              {label}
            </label>
          ))}
        </div>
        {options.airField && <Legend />}
      </div>
      <div className="hud views-bar">
        <Views layout={layout} />
      </div>
      <Inspector layout={layout} />
      <div className="hud bottom">
        <Playback />
      </div>
    </div>
  );
}

import { useMemo } from "react";
import { kelvinToCelsius, stateNumber, type Frame } from "../live/frames";
import { useSim } from "../live/store";
import { layoutHall, type HallWorld, type SceneAsset } from "../scene/hall/layout";
import { temperatureColour } from "../scene/thermal";

function css(celsius: number): string {
  if (!Number.isFinite(celsius)) return "#2a333c";
  const [r, g, b] = temperatureColour(celsius);
  return `rgb(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)})`;
}

function status(frame: Frame | null, a: SceneAsset): "out" | "fault" | "tripped" | "on" | "off" {
  if (!a.in_scope) return "out";
  if (frame?.faults.some((f) => f.target === a.id)) return "fault";
  const s = frame?.state[a.id];
  if (s?.tripped) return "tripped";
  if (s?.energised === false) return "off";
  return "on";
}

/**
 * The hall in plan: the same layout as the 3D view (racks, hot aisles, units on their walls),
 * coloured by the simulated room air and each unit's state. Light enough for any machine,
 * and what the end-to-end test drives.
 */
export function Plan({ world }: { world: HallWorld }) {
  const layout = useMemo(() => layoutHall(world), [world]);
  const frame = useSim((s) => s.frame);
  const selected = useSim((s) => s.selected);
  const select = useSim((s) => s.select);
  const room = kelvinToCelsius(stateNumber(frame, `room:${world.room.id}`, "TAir", NaN));
  const pad = 1.5;
  return (
    <svg
      className="plan"
      viewBox={`${-pad} ${-pad} ${layout.width + 2 * pad} ${layout.depth + 2 * pad}`}
      role="img"
      aria-label={`${world.room.name} plan`}
      onClick={() => select(null)}
    >
      <rect x={0} y={0} width={layout.width} height={layout.depth} className="plan-room" style={{ fill: css(room), fillOpacity: 0.16 }} />
      {layout.hotAisles.map((h) => (
        <rect key={h.z} x={h.x0} y={h.z - 0.6} width={h.x1 - h.x0} height={1.2} className="plan-aisle" style={{ fill: css(room + 6) }} />
      ))}
      {layout.racks.map((r) => (
        <rect key={`${r.z}:${r.front}`} x={r.x0} y={r.z - 0.6} width={r.count * 0.6} height={1.2} className="plan-racks" />
      ))}
      {layout.assets
        .filter((a) => a.kind !== "it-load" && a.kind !== "floor-cable")
        .map((a) => {
          const [w, d] = a.kind === "ceiling-coils" ? [a.size[0], 0.5] : [Math.max(a.size[0], 0.4), Math.max(a.size[1], 0.4)];
          const rotated = Math.abs(Math.sin(a.facing)) > 0.5;
          const [rw, rd] = rotated ? [d, w] : [w, d];
          const st = status(frame, a);
          return (
            <g
              key={a.id}
              className={`plan-asset ${st} ${a.id === selected ? "selected" : ""} kind-${a.kind}`}
              data-testid={`asset-${a.id}`}
              data-status={st}
              onClick={(e) => {
                e.stopPropagation();
                select(a.id);
              }}
            >
              <title>{`${a.name} (${a.type})`}</title>
              {a.kind === "sensor" || a.kind === "ceiling-device" ? (
                <circle cx={a.position[0]} cy={a.position[2]} r={0.28} />
              ) : (
                <rect x={a.position[0] - rw / 2} y={a.position[2] - rd / 2} width={rw} height={rd} rx={0.1} />
              )}
              {a.in_scope && (
                <text x={a.position[0]} y={a.position[2] - rd / 2 - 0.35} textAnchor="middle">
                  {a.name}
                </text>
              )}
            </g>
          );
        })}
      <text x={layout.width / 2} y={-0.5} textAnchor="middle" className="plan-title">
        {world.room.name} · {Number.isFinite(room) ? `${room.toFixed(1)} °C` : "–"}
      </text>
    </svg>
  );
}

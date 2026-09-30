import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

export interface TrendSeries {
  label: string;
  unit: string;
  values: (number | null)[];
}

const COLOURS = ["#5fd4ff", "#ffb547", "#9be15d", "#ff7ab6", "#b69cff", "#ff5a4a"];

/** Time series on shared axes, one y axis per unit (at most two). */
export function Trend({ t, series, height = 180 }: { t: number[]; series: TrendSeries[]; height?: number }) {
  const box = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const shape = series.map((s) => `${s.label}|${s.unit}`).join(";");

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const units = [...new Set(series.map((s) => s.unit))].slice(0, 2);
    const opts: uPlot.Options = {
      width: el.clientWidth || 320,
      height,
      legend: { show: true },
      cursor: { drag: { x: true, y: false } },
      scales: { x: { time: false } },
      axes: [
        { stroke: "#93a4b3", grid: { stroke: "rgba(160,200,230,0.08)" }, values: (_, ticks) => ticks.map((v) => `${Math.round(v)}s`) },
        ...units.map((u, i) => ({
          scale: u || "value",
          side: i === 0 ? 3 : 1,
          stroke: "#93a4b3",
          grid: { show: i === 0, stroke: "rgba(160,200,230,0.08)" },
          label: u,
          size: 48,
        })),
      ] as uPlot.Axis[],
      series: [
        {},
        ...series.map((s, i) => ({
          label: s.unit ? `${s.label} (${s.unit})` : s.label,
          stroke: COLOURS[i % COLOURS.length],
          width: 1.6,
          scale: units.includes(s.unit) ? s.unit || "value" : units[0] || "value",
          spanGaps: false,
        })),
      ],
    };
    plot.current = new uPlot(opts, [t, ...series.map((s) => s.values)] as uPlot.AlignedData, el);
    const resize = new ResizeObserver(() => plot.current?.setSize({ width: el.clientWidth, height }));
    resize.observe(el);
    return () => {
      resize.disconnect();
      plot.current?.destroy();
      plot.current = null;
    };
    // Rebuild only when the set of series changes; data updates go through setData below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [shape, height]);

  useEffect(() => {
    plot.current?.setData([t, ...series.map((s) => s.values)] as uPlot.AlignedData);
  }, [t, series]);

  return <div className="trend" ref={box} />;
}

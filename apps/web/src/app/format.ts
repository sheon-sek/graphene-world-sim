import type { Scalar } from "../live/frames";

export function clock(t: number): string {
  const s = Math.max(0, Math.round(t));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return `${h > 0 ? `${h}:` : ""}${String(m).padStart(h > 0 ? 2 : 1, "0")}:${String(sec).padStart(2, "0")}`;
}

/** SI state values in the units operators read: K as °C, W as kW, Pa as kPa. */
export function display(signal: string, value: Scalar, unit?: string | null): { value: string; unit: string } {
  if (typeof value === "boolean") return { value: value ? "true" : "false", unit: "" };
  if (typeof value !== "number") return { value: value === null ? "–" : String(value), unit: "" };
  if (!Number.isFinite(value)) return { value: "–", unit: "" };
  const u = unit ?? guessUnit(signal);
  if (u === "K") return { value: (value - 273.15).toFixed(1), unit: "°C" };
  if (u === "W") return { value: (value / 1000).toFixed(1), unit: "kW" };
  if (u === "Pa") return { value: (value / 1000).toFixed(1), unit: "kPa" };
  if (u === "kg/s") return { value: value.toFixed(2), unit: "kg/s" };
  const digits = Math.abs(value) >= 100 ? 0 : Math.abs(value) >= 10 ? 1 : 2;
  return { value: value.toFixed(digits), unit: u ?? "" };
}

/** The SI unit a runtime state signal is in, from the naming convention of the models. */
export function guessUnit(signal: string): string | null {
  if (/^T[A-Z]|^T$/.test(signal)) return "K";
  if (/^m\w*_flow$|_flow$/.test(signal)) return "kg/s";
  if (/^(P|Q|PFan|QEva|P_in|P_charge|demand)$/.test(signal)) return "W";
  if (/^dp/.test(signal)) return "Pa";
  if (/_pu$/.test(signal)) return "pu";
  return null;
}

export function formatValue(signal: string, value: Scalar, unit?: string | null): string {
  const d = display(signal, value, unit);
  return d.unit ? `${d.value} ${d.unit}` : d.value;
}

/** An asset id's last path segment, which is what the Ignition export shows as its name. */
export const shortName = (id: string): string => id.replace(/^~/, "").split("/").pop() ?? id;

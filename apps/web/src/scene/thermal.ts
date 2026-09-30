/**
 * Colours and the hall air-temperature field.
 *
 * The runtime models a hall as one mixed air volume (ADR-0002), so it reports one room air
 * temperature plus each cooling unit's supply and return air. The field drawn in the hall is
 * a visual distribution around those simulated values: cold near units that are blowing at
 * their supply temperature, hot in the hot aisles in proportion to the IT heat, and the room
 * temperature elsewhere. It carries no physics of its own; every input is a frame value.
 */

export type RGB = [number, number, number];

/** Temperature (degC) to colour stops: cold blue through teal and amber to red. */
const STOPS: [number, RGB][] = [
  [14, [0.1, 0.25, 0.95]],
  [18, [0.05, 0.6, 1.0]],
  [22, [0.1, 0.85, 0.75]],
  [26, [0.95, 0.85, 0.2]],
  [30, [1.0, 0.5, 0.08]],
  [36, [0.95, 0.12, 0.08]],
];

export function temperatureColour(celsius: number): RGB {
  if (!Number.isFinite(celsius)) return [0.5, 0.5, 0.5];
  if (celsius <= STOPS[0][0]) return STOPS[0][1];
  for (let i = 1; i < STOPS.length; i++) {
    const [t1, c1] = STOPS[i];
    if (celsius <= t1) {
      const [t0, c0] = STOPS[i - 1];
      const f = (celsius - t0) / (t1 - t0);
      return [c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f, c0[2] + (c1[2] - c0[2]) * f];
    }
  }
  return STOPS[STOPS.length - 1][1];
}

export interface FieldUnit {
  x: number;
  z: number;
  /** Supply air temperature, degC. */
  supply: number;
  /** Air flow as a fraction of the unit's design flow; 0 when stopped or tripped. */
  flow: number;
  /** Radius (m) the unit's supply reaches at design flow. */
  reach: number;
}

export interface FieldAisle {
  z: number;
  x0: number;
  x1: number;
}

export interface FieldInputs {
  /** Mixed room air temperature from the runtime, degC. */
  room: number;
  /** Air temperature rise across the racks, K: IT heat over the air the units move. */
  rackRise: number;
  units: FieldUnit[];
  hotAisles: FieldAisle[];
}

/** Air temperature rise across the racks from the IT heat and the cooling air flow. */
export function rackRise(itWatts: number, airKgPerS: number): number {
  const cp = 1006;
  if (airKgPerS <= 0.1) return Math.min(itWatts / (0.1 * cp), 25);
  return Math.min(itWatts / (airKgPerS * cp), 25);
}

export function fieldAt(inputs: FieldInputs, x: number, z: number): number {
  let t = inputs.room;
  for (const aisle of inputs.hotAisles) {
    const along = x < aisle.x0 ? aisle.x0 - x : x > aisle.x1 ? x - aisle.x1 : 0;
    const across = z - aisle.z;
    const w = Math.exp(-(across * across) / 0.5) * Math.exp(-(along * along) / 2);
    t += inputs.rackRise * 0.6 * w;
  }
  let pull = 0;
  let target = 0;
  for (const u of inputs.units) {
    if (u.flow <= 0.01) continue;
    const dx = x - u.x;
    const dz = z - u.z;
    const reach = u.reach * Math.sqrt(Math.min(u.flow, 1.5));
    const w = u.flow * Math.exp(-(dx * dx + dz * dz) / (reach * reach));
    pull += w;
    target += w * u.supply;
  }
  if (pull > 0) {
    const mix = Math.min(pull, 1) * 0.85;
    t = t * (1 - mix) + (target / pull) * mix;
  }
  return t;
}

/**
 * Sample the field on an `nx` by `nz` grid over a `width` by `depth` hall into RGBA bytes,
 * with faint isotherm lines every 2 K. Returns the mean temperature of the samples.
 */
export function paintField(
  inputs: FieldInputs,
  width: number,
  depth: number,
  nx: number,
  nz: number,
  out: Uint8Array,
): number {
  let sum = 0;
  for (let j = 0; j < nz; j++) {
    const z = ((j + 0.5) / nz) * depth;
    for (let i = 0; i < nx; i++) {
      const x = ((i + 0.5) / nx) * width;
      const t = fieldAt(inputs, x, z);
      sum += t;
      const [r, g, b] = temperatureColour(t);
      const band = Math.abs((t / 2) % 1 - 0.5);
      const line = band < 0.04 ? 0.35 : 0;
      const k = (j * nx + i) * 4;
      out[k] = Math.min(255, (r + line) * 255);
      out[k + 1] = Math.min(255, (g + line) * 255);
      out[k + 2] = Math.min(255, (b + line) * 255);
      out[k + 3] = 255;
    }
  }
  return sum / (nx * nz);
}

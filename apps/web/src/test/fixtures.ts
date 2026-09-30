import type { Frame } from "../live/frames";
import type { HallWorld } from "../scene/hall/layout";

/** DH01 as the World Model places it, trimmed to what the tests need. */
export const DH01: HallWorld = {
  room: { id: "DH01", name: "Data Hall DH01", floor: "Level 1", kind: "hall", x: 0, y: 0, w: 24, h: 30 },
  floor: { id: "Level 1", elevation_m: 4.5, height_m: 4.5 },
  assets: [
    { id: "FCU/L1_FCU1", name: "L1_FCU1", type: "FCU", x: 22.5, y: 13, in_scope: true, parameters: { m_air_flow_nominal: 12.43, m_wat_flow_nominal: 5.97 } },
    { id: "CRAC/L1_CRAC1", name: "L1_CRAC1", type: "CRAC", x: 22.5, y: 8, in_scope: false },
    { id: "~CCU-001", name: "CCU-001", type: "Ceiling Cooling Units", x: 12, y: 13, in_scope: true, parameters: { m_air_flow_nominal: 16.57, m_wat_flow_nominal: 7.96 } },
    { id: "~CB-001", name: "CB-001", type: "Cooling Block", x: 2, y: 27, in_scope: true },
    { id: "~IT-DH01", name: "IT-DH01", type: "IT Load", x: 18, y: 8, in_scope: true },
    { id: "BCPM/1L1", name: "1L1", type: "BCPM", x: 5, y: 28, in_scope: false },
    ...[5.8, 13.0, 20.2, 27.4].flatMap((y, i) => [
      { id: `T${i}a`, name: `Sensor ${2 * i + 1}`, type: "Temperature and Humidity", x: 4.5, y, in_scope: false },
      { id: `T${i}b`, name: `Sensor ${2 * i + 2}`, type: "Temperature and Humidity", x: 13.5, y, in_scope: false },
    ]),
  ],
};

export function frame(t: number, state: Frame["state"] = {}, faults: Frame["faults"] = []): Frame {
  return { t, step: Math.round(t / 5), state, points: {}, faults };
}

import { fieldAt, paintField, rackRise, temperatureColour, type FieldInputs } from "./thermal";

const inputs: FieldInputs = {
  room: 25,
  rackRise: 10,
  units: [{ x: 20, z: 13, supply: 18, flow: 1, reach: 6 }],
  hotAisles: [{ z: 5.8, x0: 1, x1: 19 }],
};

test("the field is the room temperature away from units and hot aisles", () => {
  expect(fieldAt(inputs, 5, 25)).toBeCloseTo(25, 1);
});

test("hot aisles run warmer and running units pull the air towards their supply", () => {
  expect(fieldAt(inputs, 10, 5.8)).toBeGreaterThan(29);
  expect(fieldAt(inputs, 20, 13)).toBeLessThan(20);
});

test("a stopped unit no longer cools the air around it", () => {
  const tripped = { ...inputs, units: [{ ...inputs.units[0], flow: 0 }] };
  expect(fieldAt(tripped, 20, 13)).toBeCloseTo(25, 1);
});

test("rack rise follows the IT heat over the air the units move", () => {
  expect(rackRise(300_000, 36)).toBeCloseTo(300_000 / (36 * 1006));
  expect(rackRise(300_000, 0)).toBe(25);
});

test("colours run from blue when cold to red when hot", () => {
  const [r0, , b0] = temperatureColour(14);
  const [r1, , b1] = temperatureColour(36);
  expect(b0).toBeGreaterThan(r0);
  expect(r1).toBeGreaterThan(b1);
  expect(temperatureColour(NaN)).toEqual([0.5, 0.5, 0.5]);
});

test("painting the field fills every texel and returns the mean", () => {
  const out = new Uint8Array(8 * 10 * 4);
  const mean = paintField(inputs, 24, 30, 8, 10, out);
  expect(mean).toBeGreaterThan(20);
  expect(mean).toBeLessThan(30);
  expect(out.every((v, i) => i % 4 !== 3 || v === 255)).toBe(true);
});

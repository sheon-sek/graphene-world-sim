import { DH01 } from "../../test/fixtures";
import { HOT_AISLE_WIDTH, layoutHall, RACK } from "./layout";

const layout = layoutHall(DH01);

test("rack rows sit back to back around each hot-aisle sensor line", () => {
  expect(layout.hotAisles.map((a) => a.z)).toEqual([5.8, 13.0, 20.2, 27.4]);
  expect(layout.racks).toHaveLength(8);
  const [a, b] = layout.racks;
  expect(b.z - a.z).toBeCloseTo(HOT_AISLE_WIDTH + RACK.d);
  expect([a.front, b.front]).toEqual([-1, 1]);
  expect(layout.rackCount).toBe(8 * layout.racks[0].count);
});

test("racks stay clear of the perimeter units and the walls", () => {
  const fcu = layout.assets.find((a) => a.id === "FCU/L1_FCU1")!;
  for (const r of layout.racks) {
    expect(r.x0).toBeGreaterThanOrEqual(1);
    expect(r.x0 + r.count * RACK.w).toBeLessThan(fcu.position[0] - fcu.size[1] / 2 - 1);
    expect(r.z - RACK.d / 2).toBeGreaterThan(0);
    expect(r.z + RACK.d / 2).toBeLessThan(DH01.room.h);
  }
});

test("perimeter units stand against the nearest wall, facing into the hall", () => {
  const fcu = layout.assets.find((a) => a.id === "FCU/L1_FCU1")!;
  expect(fcu.kind).toBe("fan-coil");
  expect(fcu.facing).toBeCloseTo(-Math.PI / 2);
  expect(fcu.position[0]).toBeGreaterThan(DH01.room.w - 1);
  expect(fcu.position[2]).toBe(13);
});

test("ceiling units hang over the nearest hot aisle and span it", () => {
  const ccu = layout.assets.find((a) => a.id === "~CCU-001")!;
  expect(ccu.position[2]).toBe(13.0);
  expect(ccu.position[1]).toBeGreaterThan(3);
  expect(ccu.size[0]).toBeGreaterThan(10);
});

test("a new asset in the World Model appears in the layout with no code change", () => {
  const grown = layoutHall({
    ...DH01,
    assets: [...DH01.assets, { id: "FCU/L1_FCU9", name: "L1_FCU9", type: "FCU", x: 22.5, y: 24, in_scope: false }],
  });
  const added = grown.assets.find((a) => a.id === "FCU/L1_FCU9")!;
  expect(added.kind).toBe("fan-coil");
  expect(added.position[2]).toBe(24);
  const unknown = layoutHall({
    ...DH01,
    assets: [...DH01.assets, { id: "X/1", name: "1", type: "Something New", x: 1, y: 1, in_scope: false }],
  });
  expect(unknown.assets.find((a) => a.id === "X/1")!.kind).toBe("perimeter");
});

test("a hall with no hot-aisle sensors still gets evenly spaced rows", () => {
  const bare = layoutHall({ ...DH01, assets: DH01.assets.filter((a) => a.type !== "Temperature and Humidity") });
  expect(bare.racks.length).toBeGreaterThanOrEqual(6);
  for (let i = 2; i < bare.racks.length; i += 2) {
    expect(bare.racks[i].z - bare.racks[i - 1].z).toBeGreaterThan(RACK.d);
  }
});

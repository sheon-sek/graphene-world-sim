import type { WorldData } from "../live/world";
import { connectionId, overlay } from "./draft";

const world = {
  revision: 1,
  site: { id: "s", name: "s" },
  types: new Map(),
  assets: new Map([["A", { id: "A", type: "FCU", name: "A" }]]),
  connections: new Map([["c1", { id: "c1", domain: "chw", label: "", source: { node: "A", port: "o" }, target: { node: "B", port: "i" } }]]),
} as unknown as WorldData;

test("a draft's operations overlay the revision", () => {
  const edited = overlay(world, [
    { op: "put", collection: "assets", value: { id: "N", type: "FCU", name: "N" } },
    { op: "delete", collection: "connections", key: "c1" },
  ]);
  expect([...edited.assets.keys()]).toEqual(["A", "N"]);
  expect(edited.connections.size).toBe(0);
  expect(edited.touched).toEqual(new Set(["N", "c1"]));
  expect(world.assets.size).toBe(1);
  expect(connectionId("air", "FCU/L1_FCU9", "room:DH01")).toBe("air:FCU/L1_FCU9->DH01");
});

import { layoutSite, routeConnections, silhouetteOf, visibleFloors } from "./layout";

const input = {
  floors: [
    { id: "Ground", index: 0, elevation_m: 0, height_m: 4.5 },
    { id: "Roof", index: 1, elevation_m: 4.5, height_m: 4.5 },
  ],
  rooms: {
    HV: { id: "HV", floor: "Ground", kind: "electrical", x: 0, y: 0, w: 20, h: 20 },
    CHP: { id: "CHP", floor: "Roof", kind: "cooling", x: 0, y: 0, w: 40, h: 40 },
  },
  assets: [
    { id: "Chiller/R_C1", name: "R_C1", type: "Chiller", location: { room: "CHP", x: 10, y: 10 } },
    { id: "UPS/UPS 1", name: "UPS 1", type: "UPS", location: { room: "HV", x: 5, y: 5 } },
    { id: "Breaker/B1", name: "B1", type: "Breaker", location: null },
    { id: "Dashboard/X", name: "X", type: "Dashboard", location: null },
  ],
  links: [["Breaker/B1", "UPS/UPS 1"]] as [string, string][],
};

describe("site layout", () => {
  const site = layoutSite(input);
  const by = (id: string) => site.assets.find((a) => a.id === id)!;

  it("places every asset, on its floor's elevation", () => {
    expect(site.assets).toHaveLength(4);
    expect(by("Chiller/R_C1").position).toEqual([10, 4.5, 10]);
    expect(by("Chiller/R_C1").system).toBe("cooling");
  });

  it("sets an unplaced asset beside the asset it connects to, and the rest in a row outside", () => {
    const breaker = by("Breaker/B1");
    expect(breaker.placed).toBe(false);
    expect(breaker.floor).toBe("Ground");
    expect(Math.hypot(breaker.position[0] - 5, breaker.position[2] - 5)).toBeLessThan(3);
    expect(by("Dashboard/X").position[2]).toBeLessThan(0);
  });

  it("cuts away the floors above the one looked at", () => {
    expect([...visibleFloors(site, "Ground")]).toEqual(["Ground"]);
    expect(visibleFloors(site, null).size).toBe(2);
  });

  it("routes a connection between floors through its domain's shaft", () => {
    const [route] = routeConnections(
      site,
      [{ id: "c", domain: "power", source: { node: "UPS/UPS 1" }, target: { node: "Chiller/R_C1" } }],
      { EL: { x: 30, y: 30, carries: ["power"] } },
    );
    expect(route.points.some(([x, , z]) => x === 30 && z === 30)).toBe(true);
    expect(route.points.at(-1)).toEqual([10, 4.5, 10]);
  });

  it("gives every known type a silhouette", () => {
    expect(silhouetteOf("Cooling Tower").size[2]).toBeGreaterThan(3);
    expect(silhouetteOf("Something new").shape).toBe("box");
  });
});

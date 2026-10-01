import { href, parseRoute } from "./router";

describe("routes", () => {
  it("parse and print the session workspaces", () => {
    expect(parseRoute("#/s/R1/engineering")).toEqual({ name: "session", session: "R1", workspace: "engineering" });
    expect(parseRoute("#/s/R1")).toEqual({ name: "session", session: "R1", workspace: "operations" });
    expect(parseRoute("")).toEqual({ name: "sessions" });
    expect(parseRoute("#/hero")).toEqual({ name: "hero" });
    expect(href({ name: "session", session: "R 2", workspace: "diagnostics" })).toBe("#/s/R%202/diagnostics");
    expect(parseRoute("#/s/R1/opcua")).toEqual({ name: "session", session: "R1", workspace: "opcua" });
  });
});

import { render, screen } from "@testing-library/react";
import { vi } from "vitest";

vi.mock("./scene/HallView", () => ({ HallView: () => null, frameStats: { fps: 0 } }));
vi.mock("uplot", () => ({ default: class {} }));

import { App } from "./App";

const PRESET = {
  id: "dh01-slice",
  name: "DH01 cooling slice",
  description: "Chiller 1 and the DH01 units.",
  scope: ["FCU/L1_FCU1"],
  dt: 5,
  room: "DH01",
  conditions: {},
};

function serve(routes: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(input instanceof Request ? input.url : String(input));
      const body = routes[url.pathname];
      return body === undefined
        ? new Response(JSON.stringify({ detail: "offline" }), { status: 503 })
        : new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

afterEach(() => {
  window.location.hash = "";
});

test("opens on the sessions page with the presets to start from", async () => {
  serve({ "/api/runtime/sessions": [], "/api/runtime/presets": [PRESET] });
  render(<App />);
  expect(await screen.findByText("DH01 cooling slice")).toBeTruthy();
  expect(await screen.findByText("No sessions yet.")).toBeTruthy();
});

test("#/hero opens the hero data hall and loads its recording", async () => {
  serve({});
  window.location.hash = "#/hero";
  render(<App />);
  expect(screen.getByText("Loading the hall…")).toBeTruthy();
  expect(await screen.findByText(/Could not load the hall/)).toBeTruthy();
});

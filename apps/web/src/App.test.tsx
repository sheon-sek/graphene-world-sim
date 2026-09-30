import { render, screen } from "@testing-library/react";
import { vi } from "vitest";

vi.mock("./scene/HallView", () => ({ HallView: () => null, frameStats: { fps: 0 } }));

import { App } from "./App";

test("opens on the hero data hall and loads its recording", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ error: "offline" }), { status: 503 })),
  );
  render(<App />);
  expect(screen.getByText("Loading the hall…")).toBeTruthy();
  expect(await screen.findByText(/Could not load the hall/)).toBeTruthy();
});

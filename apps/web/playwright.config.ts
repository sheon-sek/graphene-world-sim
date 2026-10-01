import { defineConfig, devices } from "@playwright/test";

/**
 * The Phase 5 gate (#58): the whole engineering-to-operations workflow in a browser, against
 * the real API and runtime. `GWS_E2E_URL` points at a running `gws_api.serve --web dist`;
 * without it, one is started on a fresh World Model store. The 3D view is off (`?3d=0`):
 * software rendering in CI is too slow for it, and the plan view shows the same state.
 */
const external = process.env.GWS_E2E_URL;
const port = Number(process.env.GWS_E2E_PORT ?? 8765);

export default defineConfig({
  testDir: "e2e",
  timeout: 30 * 60_000,
  expect: { timeout: 20_000 },
  retries: 0,
  workers: 1,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: external ?? `http://127.0.0.1:${port}`,
    actionTimeout: 30_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    viewport: { width: 1600, height: 1000 },
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1600, height: 1000 } } }],
  webServer: external
    ? undefined
    : {
        command: `rm -f e2e-world.sqlite && cd ../.. && uv run python -m gws_api.serve --db apps/web/e2e-world.sqlite --import-graphene data/graphene --start none --no-opcua --web apps/web/dist --port ${port}`,
        url: `http://127.0.0.1:${port}/api/runtime/presets`,
        timeout: 120_000,
        reuseExistingServer: false,
        stdout: "ignore",
        stderr: "pipe",
      },
});

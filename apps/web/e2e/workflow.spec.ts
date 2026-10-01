import { expect, test, type Page } from "@playwright/test";

/**
 * add → configure → connect → run → fault → observe → recover, on the DH01 slice.
 *
 * A second fan-coil unit is added to DH01, given a smaller capacity, connected to cooling
 * block 1, the hall air and FCU1's meter, and applied as a new revision; the session is
 * rebuilt on it (which compiles the changed partition). Then FCU1 is tripped: its alarms
 * rise, the hall warms and the propagation timeline shows where the consequences went.
 * Clearing and resetting the trip returns FCU1 to service and clears its alarms.
 *
 * It expects a store without FCU9 (the configured server starts on a fresh one).
 */

const NEW = "FCU/L1_FCU9";
const FCU1 = "FCU/L1_FCU1";
const COMPILE = 20 * 60_000;

async function roomAir(page: Page): Promise<number> {
  return page.evaluate(() => {
    const frame = window.gws?.store.getState().frame;
    const t = frame?.state["room:DH01"]?.TAir;
    return typeof t === "number" ? t - 273.15 : NaN;
  });
}

async function step60(page: Page, times = 1) {
  for (let i = 0; i < times; i += 1) {
    const before = await page.getByTestId("sim-clock").textContent();
    await page.getByRole("button", { name: "+60" }).click();
    await expect(page.getByTestId("sim-clock")).not.toHaveText(before ?? "");
  }
}

test("add, configure, connect, run, fault, observe and recover", async ({ page }) => {
  page.on("pageerror", (e) => console.log(`page error: ${e.message}`));

  // A session on the slice.
  await page.goto("./?3d=0#/");
  await page.getByTestId("preset-dh01-slice").getByRole("button", { name: "Start session" }).click();
  await expect(page).toHaveURL(/#\/s\/[^/]+\/operations/, { timeout: COMPILE });
  await expect(page.getByTestId("session-chip")).toContainText("18 assets");
  await expect(page.getByTestId(`asset-${FCU1}`)).toHaveAttribute("data-status", "on");

  // Add: a fan-coil unit in DH01.
  await page.getByRole("link", { name: "Engineering" }).click();
  await page.getByTestId("type-FCU").click();
  await page.getByTestId("add-name").fill("L1_FCU9");
  await page.getByTestId("add-confirm").click();
  await expect(page.getByTestId(`node-${NEW}`)).toBeVisible();
  await expect(page.getByTestId("properties")).toContainText(NEW);

  // Configure: 120 kW instead of the type's 150 kW.
  await page.getByTestId("param-q_flow_nominal").fill("120");
  await page.getByTestId("stage-asset").click();
  await expect(page.getByTestId("draft-panel")).toContainText("2 operations");

  // Connect: chilled water from cooling block 1, supply air into the hall, power from FCU1's meter.
  const connect = async (port: string, option: string) => {
    await page.getByTestId(`connect-${port}`).selectOption({ label: option });
    await page.getByTestId(`connect-${port}-go`).click();
    await expect(page.getByTestId(`port-${port}`)).toContainText(option.replace(/^(from|to) /, ""));
  };
  await connect("chw_in", "from CB-001.chw_out");
  await connect("air_out", "to room DH01.air");
  await connect("power_in", "from Meter11.power_out");
  await expect(page.getByTestId("validation")).toHaveText(/^Valid/);
  await expect(page.getByTestId("diff")).toContainText("structural");

  // Apply, and rebuild the session on the new revision with the new unit in scope.
  await page.getByTestId("apply-message").fill("Add FCU9 to DH01");
  await page.getByTestId("apply-draft").click();
  await expect(page.getByTestId("applied")).toContainText(/Revision \d+ applied/);
  const revision = (await page.getByTestId("applied").textContent())?.match(/Revision (\d+)/)?.[1];
  await page.getByTestId("reinit").click();
  await expect(page.getByTestId("session-chip")).toContainText(`rev ${revision}`, { timeout: COMPILE });
  await expect(page.getByTestId("session-chip")).toContainText("19 assets");

  // Run: settle for ten simulated minutes.
  await page.getByRole("link", { name: "Operations" }).click();
  await expect(page.getByTestId(`asset-${NEW}`)).toBeVisible();
  await step60(page, 2);
  await page.getByTestId(`asset-${NEW}`).click();
  await expect(page.getByTestId("inspector")).toContainText("L1_FCU9");
  await expect(page.getByTestId("inspector").locator('[data-signal="energised"]')).toHaveText("true");
  const settled = await roomAir(page);
  expect(settled).toBeGreaterThan(15);

  // Fault: trip FCU1.
  await page.getByTestId(`asset-${FCU1}`).click();
  await page.getByTestId("fault-target").selectOption(FCU1);
  await page.getByTestId("fault-mode").selectOption("trip");
  await page.getByTestId("inject").click();
  await expect(page.getByTestId("active-fault")).toHaveCount(1);
  await step60(page, 2);

  // Observe: FCU1 tripped and alarming, the hall warmer, the consequences traced.
  await expect(page.getByTestId(`asset-${FCU1}`)).toHaveAttribute("data-status", "fault");
  await expect(page.getByTestId("active-alarms")).toBeVisible();
  await expect(page.getByTestId("alarm").first()).toContainText("L1_FCU1");
  await expect(page.getByTestId("alarm").first()).toContainText("after fault trip on l1_fcu1");
  expect(await roomAir(page)).toBeGreaterThan(settled + 0.3);
  const reached = page.getByTestId("reached");
  await expect(reached.filter({ hasText: "L1_FCU1" })).toHaveCount(1);
  await expect(reached.filter({ hasText: "Room DH01" })).toHaveCount(1);

  // Recover: clear the cause, reset the latched trip, and let the hall settle.
  const fault = page.getByTestId("active-fault");
  await fault.getByRole("button", { name: "Clear" }).click();
  await expect(fault).toContainText("latched until reset");
  await fault.getByRole("button", { name: "Reset" }).click();
  await expect(page.getByTestId("active-fault")).toHaveCount(0);
  await step60(page, 3);
  await expect(page.getByTestId(`asset-${FCU1}`)).toHaveAttribute("data-status", "on");
  await expect(page.getByTestId("active-alarms")).toHaveCount(0);
});

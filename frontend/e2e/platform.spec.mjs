import { expect, test } from "@playwright/test";
import { admin, json } from "../../scripts/simulator.mjs";

const simulator = process.env.BUP_TEST_SIMULATOR;

test("upgraded dashboard views, optimizer what-if, keyboard nav, and mobile layout work", async ({ page }) => {
  await admin(simulator, "reset");
  for (let i = 0; i < 36; i++) {
    await admin(simulator, "step");
  }

  await page.goto("/");
  await expect(page.getByRole("navigation", { name: "Operations views" })).toBeVisible();
  await expect(page.locator(".updated")).toContainText("as of t36 / sim t36");

  // Keyboard navigation across tabs
  const networkTab = page.getByRole("button", { name: "Network" });
  await networkTab.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("region", { name: "Network topology and routes" })).toBeVisible();
  await expect(page.getByRole("img", { name: "Fuel depot to station topology diagram" })).toBeVisible();

  // Demand forecast explorer with p10-p90 quantile SVG
  await page.getByRole("button", { name: "Forecasts" }).click();
  await expect(page.getByRole("region", { name: "Demand forecast explorer" })).toBeVisible();
  await expect(page.getByRole("img", { name: "Demand history and quantile forecast chart" })).toBeVisible();

  // Shortages & Incidents tab
  await page.getByRole("button", { name: "Shortages & Incidents" }).click();
  await expect(page.getByRole("region", { name: "Shortage projection and incident timeline" })).toBeVisible();
  await expect(page.getByText("Uncalibrated Heuristic")).toBeVisible();

  // Optimizer & What-If tab (OR-Tools joint optimization + counterfactual simulate + line-item batch review + tick+1 replay safety)
  await page.getByRole("button", { name: "Optimizer & What-If" }).click();
  const optRegion = page.getByRole("region", { name: "Joint optimization and what-if planner" });
  await expect(optRegion).toBeVisible();
  await optRegion.getByRole("button", { name: "Run Optimizer" }).click();
  await expect(optRegion.getByText("NO-ACTION UNMET")).toBeVisible();
  await expect(optRegion.getByRole("region", { name: "Joint plan line-item review" })).toBeVisible();
  await optRegion.getByRole("button", { name: "Simulate" }).click();
  await expect(optRegion.getByText(/Feasible proposal|Invalid:/)).toBeVisible();
  await optRegion.getByRole("spinbutton", { name: "Counterfactual quantity" }).fill("-1");
  const rejectedPreview = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/simulate" && response.request().method() === "POST");
  await optRegion.getByRole("button", { name: "Simulate", exact: true }).click();
  expect((await rejectedPreview).status()).toBe(422);
  await expect(optRegion.getByRole("alert")).toContainText("Could not simulate shipment. Request failed (422)");
  await expect(optRegion.getByText(/Feasible proposal|Invalid:/)).toHaveCount(0);
  await optRegion.getByRole("spinbutton", { name: "Counterfactual quantity" }).fill("3000");
  await optRegion.getByRole("button", { name: "Simulate", exact: true }).click();
  await expect(optRegion.getByText(/Feasible proposal|Invalid:/)).toBeVisible();
  await expect(optRegion.getByRole("alert")).toHaveCount(0);

  // Approve 10-shipment joint batch via backend-owned PUT /api/plans/{plan_id}/approval
  const reviewBtn = optRegion.getByRole("button", { name: /Review joint batch \(/ });
  await expect(reviewBtn).toBeVisible();
  await reviewBtn.click();
  await expect(optRegion.getByRole("region", { name: "Joint batch approval confirmation" })).toBeVisible();
  const planPut = page.waitForResponse(
    (r) => r.url().includes("/api/plans/") && r.url().endsWith("/approval") && r.request().method() === "PUT",
  );
  await optRegion.getByRole("button", { name: /Confirm & Submit Batch \(/ }).click();
  const batchRes = await (await planPut).json();
  expect(batchRes.state).toBe("CONFIRMED");
  expect(batchRes.confirmed_count).toBeGreaterThanOrEqual(8);
  const initialCount = batchRes.confirmed_count;
  const simAllocs1 = await json(`${simulator}/v1/allocations`);
  expect(simAllocs1).toHaveLength(initialCount);

  // Browser refresh preserves server-stored plan & durable per-shipment allocation IDs
  await page.reload();
  await page.getByRole("button", { name: "Optimizer & What-If" }).click();
  await expect(optRegion.getByText(`Batch status: CONFIRMED — ${initialCount}/${initialCount} confirmed`)).toBeVisible();

  // Step simulator from tick 36 to tick 37 and replay same plan approval: STILL exact same count!
  await admin(simulator, "step");
  const replayBtn = optRegion.getByRole("button", { name: /All \d+ confirmed \(replay safe\)/ });
  await expect(replayBtn).toBeVisible();
  const replayPut = page.waitForResponse(
    (r) => r.url().includes("/api/plans/") && r.url().endsWith("/approval") && r.request().method() === "PUT",
  );
  await replayBtn.click();
  const replayRes = await (await replayPut).json();
  expect(replayRes.state).toBe("CONFIRMED");
  expect(replayRes.confirmed_count).toBe(initialCount);
  const simAllocs2 = await json(`${simulator}/v1/allocations`);
  expect(simAllocs2).toHaveLength(initialCount);
  expect(simAllocs2.map((a) => a.id).sort((a, b) => a - b)).toEqual(simAllocs1.map((a) => a.id).sort((a, b) => a - b));

  // Models & Eval tab
  await page.getByRole("button", { name: "Models & Eval" }).click();
  await expect(page.getByRole("region", { name: "Models, generation and benchmark evaluation" })).toBeVisible();

  await test.step("Keep the dashboard views within 390 and 320 pixel viewports", async () => {
    const pageWidth = () => page.evaluate(() => document.documentElement.scrollWidth);
    const views = [
      ["Overview", [page.getByRole("heading", { name: "Inventory by location" })]],
      ["Network", [page.getByRole("img", { name: "Fuel depot to station topology diagram" })]],
      ["Forecasts", [page.getByRole("img", { name: "Demand history and quantile forecast chart" })]],
      ["Shortages & Incidents", [page.getByRole("region", { name: "Shortage projection and incident timeline" }).getByRole("row").nth(1)]],
      ["Optimizer & What-If", [optRegion.getByRole("button", { name: "Run Optimizer" }), optRegion.getByRole("button", { name: "Refresh batch status" })]],
      ["Models & Eval", [page.getByRole("region", { name: "Models, generation and benchmark evaluation" })]],
    ];
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      for (const [view, contents] of views) {
        await page.getByRole("navigation", { name: "Operations views" }).getByRole("button", { name: view, exact: true }).click();
        for (const content of contents) await expect(content).toBeVisible();
        await expect.poll(pageWidth, { message: `${view} should fit at ${width}px` }).toBe(width);
      }
    }
  });
});

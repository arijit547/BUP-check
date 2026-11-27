import { expect, test } from "@playwright/test";
import { admin, json, seedHistory } from "../../scripts/simulator.mjs";

const simulator = process.env.BUP_TEST_SIMULATOR;

test("stale data pauses batch controls and a stopped batch restores its line-item outcomes", async ({ page }, testInfo) => {
  await admin(simulator, "reset");
  await seedHistory(simulator);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await page.getByRole("button", { name: "Optimizer & What-If" }).click();

  const planner = page.getByRole("region", { name: "Joint optimization and what-if planner" });
  const lineItems = planner.getByRole("region", { name: "Joint plan line-item review" });
  const review = planner.getByRole("region", { name: "Joint batch approval confirmation" });
  const optimize = planner.getByRole("button", { name: "Run Optimizer" });
  const simulate = planner.getByRole("button", { name: "Simulate", exact: true });
  const confirm = review.getByRole("button", { name: /Confirm & Submit Batch/ });
  const snapshotStatus = page.locator(".updated small");
  const optimized = page.waitForResponse(response =>
    new URL(response.url()).pathname === "/api/optimize" && response.request().method() === "POST");
  await optimize.click();
  const plan = await (await optimized).json();
  expect(plan.shipments.length).toBeGreaterThan(2);
  const blockedIndex = plan.shipments.findIndex(shipment => shipment.route_id !== plan.shipments[0].route_id);
  expect(blockedIndex).toBeGreaterThan(0);
  expect(blockedIndex).toBeLessThan(plan.shipments.length - 1);
  await planner.getByRole("button", { name: /Review joint batch/ }).click();
  await expect(confirm).toBeEnabled();
  await testInfo.attach("mobile-batch-review", { body: await review.screenshot(), contentType: "image/png" });

  await test.step("Disable optimizer, preview, and confirmation while retaining the reviewed plan", async () => {
    await admin(simulator, "faults", { type: "stale_data", duration_seconds: 90 });
    try {
      await expect(snapshotStatus).toContainText("Stale · last validated");
      await expect(confirm).toBeDisabled();
      await expect(optimize).toBeDisabled();
      await expect(simulate).toBeDisabled();
      await expect(lineItems.getByRole("row")).toHaveCount(plan.shipments.length + 1);
      expect((await page.request.get("/api/snapshot")).status()).toBe(503);
      expect(await json(`${simulator}/v1/allocations`)).toHaveLength(0);
    } finally {
      await admin(simulator, "faults/clear");
    }
    await expect(snapshotStatus).toContainText("Updated at");
    await expect(confirm).toBeEnabled();
    await expect(optimize).toBeEnabled();
    await expect(simulate).toBeEnabled();
  });

  await test.step("Record the confirmed, rejected, and unsent shipments across reload", async () => {
    const blockedRoute = plan.shipments[blockedIndex].route_id;
    await admin(simulator, "events", {
      type: "route_disruption", start_tick: plan.as_of_tick,
      duration_ticks: 12, parameters: { route_ids: [blockedRoute] },
    });
    await admin(simulator, "step");
    expect((await json(`${simulator}/v1/routes`)).find(route => route.id === blockedRoute).status).toBe("DISRUPTED");
    const approved = page.waitForResponse(response =>
      new URL(response.url()).pathname === `/api/plans/${plan.plan_id}/approval` && response.request().method() === "PUT");
    await confirm.click();
    const response = await approved;
    expect(response.ok()).toBe(true);
    const result = await response.json();
    const unsent = plan.shipments.length - blockedIndex - 1;
    expect(result).toMatchObject({
      state: "STOPPED", confirmed_count: blockedIndex, rejected_count: 1,
      unsent_count: unsent, requires_new_plan: true,
    });
    expect(result.stopped_reason).toMatch(/route is disrupted/i);
    const allocationIds = async () => (await json(`${simulator}/v1/allocations`)).map(item => item.id).sort((a, b) => a - b);
    const initialIds = await allocationIds();
    const confirmedIds = result.items.flatMap(item => item.shipment ? [item.shipment.id] : []).sort((a, b) => a - b);
    expect(initialIds).toHaveLength(blockedIndex);
    expect(initialIds).toEqual(confirmedIds);

    const status = planner.getByRole("status");
    const summary = `Batch status: STOPPED — ${blockedIndex}/${plan.shipments.length} confirmed, 1 rejected, ${unsent} unsent`;
    const replan = planner.getByRole("button", { name: "Generate updated plan" });
    await expect(status).toContainText(summary);
    await expect(status).toContainText(result.stopped_reason);
    await expect(replan).toBeEnabled();
    await expect(lineItems.getByRole("row").nth(blockedIndex + 1)).toContainText("REJECTED");
    await expect(lineItems.getByText("UNSENT", { exact: true })).toHaveCount(unsent);

    await page.reload();
    await page.getByRole("button", { name: "Optimizer & What-If" }).click();
    await expect(status).toContainText(summary);
    await expect(replan).toBeEnabled();
    await expect(lineItems.getByText(`#${initialIds[0]} · PENDING`, { exact: true })).toBeVisible();
    expect(await allocationIds()).toEqual(initialIds);
    await testInfo.attach("stopped-batch", { body: await status.screenshot(), contentType: "image/png" });
    await testInfo.attach("stopped-batch-result", {
      body: JSON.stringify({ blockedRoute, initialIds, result }, null, 2), contentType: "application/json",
    });
  });
});

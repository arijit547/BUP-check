import { test, expect } from "@playwright/test";
import { admin, json, seedHistory } from "../../scripts/simulator.mjs";

const simulator = process.env.BUP_TEST_SIMULATOR;
const dashboard = process.env.BUP_TEST_DASHBOARD;

test("operator reviews a crisis recommendation, approves, tracks delivery and recovers", async ({ page }, testInfo) => {
  let first;
  await test.step("Seed real demand and disrupt a recommended route", async () => {
    await seedHistory(simulator);
    const baseline = await json(`${dashboard}/api/planning`);
    const closedRoute = baseline.recommendations[0].route_id;
    await admin(simulator, "events", {
      type: "route_disruption", start_tick: baseline.tick,
      duration_ticks: 12, parameters: { route_ids: [closedRoute] },
    });
    await admin(simulator, "step");

    const planning = await json(`${dashboard}/api/planning`);
    expect(planning.forecasts).toHaveLength(12);
    expect(planning.forecasts.every(item => item.observations === planning.history_ticks)).toBe(true);
    expect(planning.recommendations.length).toBeGreaterThan(0);
    expect(planning.recommendations.map(item => item.route_id)).not.toContain(closedRoute);
    expect((await json(`${simulator}/v1/routes`)).find(route => route.id === closedRoute).status).toBe("DISRUPTED");
    first = planning.recommendations[0];
    await testInfo.attach("planning-after-disruption", {
      body: JSON.stringify({ tick: planning.tick, closedRoute, first }, null, 2),
      contentType: "application/json",
    });
  });

  const approvals = page.getByRole("region", { name: "Approval history" });
  const reviews = page.getByRole("button", { name: "Review", exact: true });
  const lastValidated = page.locator(".updated small");
  let allocation;
  await test.step("Verify the live inventory and approve the displayed proposal", async () => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Inventory by location" })).toBeVisible();
    await expect(page.getByRole("region", { name: "Station and fuel forecasts" }).getByRole("row")).toHaveCount(13);
    await expect(page.getByRole("region", { name: "System health and observability" })).toContainText("SYSTEM OK");
    await expect(page.getByRole("region", { name: "Anomalies and incoming supply" })).toContainText("ROUTE DISRUPTED");
    await expect(reviews.first()).toBeEnabled();
    await reviews.first().click();
    await expect(page.getByText("Review shipment", { exact: true })).toBeVisible();
    await expect(page.getByText(/Signals:/)).toBeVisible();
    const approvalResponse = page.waitForResponse(response =>
      response.url().includes("/api/approvals/") && response.request().method() === "PUT");
    await page.getByRole("button", { name: "Approve shipment", exact: true }).click();
    const result = await (await approvalResponse).json();
    expect(result.state).toBe("CONFIRMED");
    [allocation] = await json(`${simulator}/v1/allocations`);
    expect(await json(`${simulator}/v1/allocations`)).toHaveLength(1);
    expect(allocation).toMatchObject({
      id: result.shipment.id, route_id: first.route_id, fuel_type: first.fuel_type,
      quantity: first.quantity_liters, status: "PENDING",
    });
    await expect(approvals).toContainText(`#${allocation.id} · PENDING`);
  });

  await test.step("Track the official allocation through arrival", async () => {
    await admin(simulator, "step");
    const [transit] = await json(`${simulator}/v1/allocations`);
    expect(transit.status).toBe("IN_TRANSIT");
    await expect(approvals).toContainText(`#${allocation.id} · IN TRANSIT`);
    for (let tick = 0; tick < first.eta_ticks; tick++) await admin(simulator, "step");
    await expect(approvals).toContainText(`#${allocation.id} · ARRIVED`);
    const [arrived] = await json(`${simulator}/v1/allocations`);
    expect(arrived.status).toBe("ARRIVED");
    await testInfo.attach("arrived-allocation", {
      body: JSON.stringify(arrived, null, 2), contentType: "application/json",
    });
  });

  await test.step("Retain the last validated view during stale data and recover", async () => {
    await expect(reviews.first()).toBeEnabled();
    await admin(simulator, "faults", { type: "stale_data", duration_seconds: 90 });
    try {
      const baselineTime = (await lastValidated.innerText()).split(" at ").at(-1);
      await expect(lastValidated).toHaveText(`Stale · last validated at ${baselineTime}`);
      await expect(page.getByRole("heading", { name: "Inventory by location" })).toBeVisible();
      await expect(approvals).toContainText(`#${allocation.id} · ARRIVED`);
      await expect.poll(async () => reviews.evaluateAll(buttons =>
        buttons.length > 0 && buttons.every(button => button.disabled))).toBe(true);
      expect((await page.request.get("/api/snapshot")).status()).toBe(503);
      await testInfo.attach("stale-dashboard", { body: await page.screenshot(), contentType: "image/png" });
    } finally {
      await admin(simulator, "faults/clear");
    }
    await expect(lastValidated).toContainText("Updated at");
    await expect(reviews.first()).toBeEnabled();
    expect(await json(`${simulator}/v1/allocations`)).toHaveLength(1);
  });
});

import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium } from "../frontend/node_modules/playwright/index.mjs";

const url = process.env.DASH_URL ?? "http://127.0.0.1:3000";
const browserPath = process.env.BUP_CHROMIUM_PATH ?? "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const outDir = "artifacts/live-check";

await mkdir(outDir, { recursive: true });
const browser = await chromium.launch({ executablePath: browserPath, headless: true });
const consoleErrors = [];
const pageErrors = [];
const failedRequests = [];

try {
  const context = await browser.newContext({ viewport: { width: 1440, height: 920 } });
  const page = await context.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text());
  });
  page.on("pageerror", (err) => pageErrors.push(String(err)));
  page.on("response", (resp) => {
    if (resp.status() >= 400 && !resp.url().includes("favicon")) {
      failedRequests.push(`${resp.status()} ${resp.url()}`);
    }
  });

  await page.goto(url, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "Fuel network overview" }).waitFor();
  await page.getByText("TICK 036").waitFor();
  await page.getByRole("region", { name: "Station and fuel forecasts" }).getByRole("row").nth(12).waitFor();
  await page.screenshot({ path: `${outDir}/tab-overview.png`, fullPage: false });

  await page.getByRole("button", { name: "Network" }).click();
  await page.getByRole("img", { name: "Fuel depot to station topology diagram" }).waitFor();
  await page.screenshot({ path: `${outDir}/tab-network.png`, fullPage: false });

  await page.getByRole("button", { name: "Forecasts" }).click();
  await page.getByLabel("Forecast policy override").selectOption("CHRONOS2");
  await page.getByText("Source: CHRONOS2").waitFor({ timeout: 15000 });
  await page.getByRole("img", { name: "Demand history and quantile forecast chart" }).waitFor();
  await page.screenshot({ path: `${outDir}/tab-forecasts-chronos2.png`, fullPage: false });

  await page.getByRole("button", { name: "Shortages & Incidents" }).click();
  await page.getByText("Uncalibrated Heuristic").waitFor();
  await page.screenshot({ path: `${outDir}/tab-shortages.png`, fullPage: false });

  await page.getByRole("button", { name: "Optimizer & What-If" }).click();
  await page.getByRole("button", { name: "Run Optimizer" }).click();
  await page.getByText("NO-ACTION UNMET").waitFor({ timeout: 10000 });
  await page.getByRole("button", { name: "Simulate" }).click();
  await page.getByText(/Feasible proposal|Invalid:/).waitFor({ timeout: 10000 });
  await page.screenshot({ path: `${outDir}/tab-optimizer.png`, fullPage: false });

  await page.getByRole("button", { name: "Models & Eval" }).click();
  await page.getByText(/ready \(Chronos-2: loaded/i).waitFor({ timeout: 10000 });
  await page.getByText("ortools_seasonal_batch_unconstrained").waitFor();
  await page.screenshot({ path: `${outDir}/tab-models-eval.png`, fullPage: false });

  // Mobile layout check
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Overview" }).click();
  await page.getByRole("heading", { name: "Inventory by location" }).waitFor();
  await page.screenshot({ path: `${outDir}/mobile-overview.png`, fullPage: false });

  assert.deepEqual(consoleErrors, [], `Console errors: ${consoleErrors.join("; ")}`);
  assert.deepEqual(pageErrors, [], `Page errors: ${pageErrors.join("; ")}`);
  assert.deepEqual(failedRequests, [], `Failed HTTP requests: ${failedRequests.join("; ")}`);
  console.log(JSON.stringify({ live_ui_verified: true, consoleErrors: 0, pageErrors: 0, failedRequests: 0, tabsChecked: 6 }));
} finally {
  await browser.close();
}

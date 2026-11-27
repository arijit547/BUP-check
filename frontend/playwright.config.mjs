import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

const { BUP_TEST_DASHBOARD: dashboard, BUP_TEST_SIMULATOR: simulator, BUP_TEST_OUTPUT: output } = process.env;
if (!dashboard || !simulator || !output) {
  throw new Error("Run node scripts/verify.mjs e2e or set BUP_TEST_DASHBOARD, BUP_TEST_SIMULATOR and BUP_TEST_OUTPUT for a disposable stack.");
}
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  globalTimeout: 180_000,
  expect: { timeout: 20_000 },
  workers: 1,
  forbidOnly: !!process.env.CI,
  outputDir: resolve(output, "test-results"),
  reporter: [
    ["list"],
    ["json", { outputFile: resolve(output, "playwright.json") }],
  ],
  use: {
    baseURL: dashboard,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...(process.env.BUP_CHROMIUM_PATH
      ? { launchOptions: { executablePath: process.env.BUP_CHROMIUM_PATH } }
      : {}),
  },
});

import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";

const output = process.env.BUP_TEST_OUTPUT;
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
    baseURL: process.env.BUP_TEST_DASHBOARD,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...(process.env.BUP_CHROMIUM_PATH
      ? { launchOptions: { executablePath: process.env.BUP_CHROMIUM_PATH } }
      : {}),
  },
});

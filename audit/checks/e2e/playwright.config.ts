import { defineConfig, devices } from "@playwright/test";

// Audit e2e config. Runs against the live web (:5173) + api (:8000). Reuses the
// @playwright/test install under tests/playwright (invoke playwright from there).
const BASE_URL = process.env.ALEPH_WEB_BASE_URL ?? "http://localhost:5173";
const API_URL = process.env.ALEPH_API_BASE_URL ?? "http://localhost:8000";

export default defineConfig({
  testDir: ".",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: [["line"]],
  use: {
    baseURL: BASE_URL,
    extraHTTPHeaders: { Authorization: "Bearer local-dev" },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
  ],
});

export { BASE_URL, API_URL };

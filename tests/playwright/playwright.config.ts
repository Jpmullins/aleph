import { defineConfig, devices } from "@playwright/test";

const BASE_URL = process.env.ALEPH_WEB_BASE_URL ?? "http://localhost:5173";
const API_URL = process.env.ALEPH_API_BASE_URL ?? "http://localhost:8000";

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false, // ledger + project state is shared
  workers: 1,
  retries: 0,
  // Per-test timeout. Source→wiki pipeline involves ~30s embedding +
  // ~80s wiki ingest, so leave generous headroom.
  timeout: 300_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: BASE_URL,
    extraHTTPHeaders: {
      // Local-mode auth: API JIT-provisions dev@aleph.local. The sentinel
      // bearer is recognized by AuthMiddleware in local mode.
      Authorization: "Bearer local-dev",
    },
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
});

export { BASE_URL, API_URL };

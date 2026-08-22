import { defineConfig, devices } from "@playwright/test";

/**
 * The browser suite, pointed at a running stack.
 *
 * Both base URLs come from the environment because the compose stack does not
 * publish on the ports the app defaults to: `deploy/compose/docker-compose.yml`
 * maps web to 5273 and the runtime bridge to 127.0.0.1:4100. Hardcoding 5173
 * here is how a suite ends up "passing" locally against a dev server somebody
 * started by hand and failing everywhere else.
 *
 *   ALEPH_WEB_BASE_URL=http://localhost:5273 \
 *   ALEPH_API_BASE_URL=http://localhost:8000 \
 *   pnpm -C tests/playwright test
 *
 * `retries: 0` is deliberate and is the rule from docs/plan.md WS-P4's risk
 * note: a test that needs a retry is a defect in the test, not a fact of life.
 * A flaky required job trains everyone to re-run rather than read.
 */
const BASE_URL = process.env.ALEPH_WEB_BASE_URL ?? "http://localhost:5173";
const API_URL = process.env.ALEPH_API_BASE_URL ?? "http://localhost:8000";

export default defineConfig({
  testDir: "./specs",
  // The specs create and delete real projects through the API. Run them one at
  // a time: `cleanupAuditProjects` deletes every project carrying the e2e title
  // prefix, which would tear down a sibling worker's fixture mid-test.
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: [["line"]],
  use: {
    baseURL: BASE_URL,
    // `local-dev` is the sentinel bearer the API recognises as the local
    // principal (apps/web/src/lib/auth.ts). Sent rather than omitted so the
    // request travels the same middleware a real one would.
    extraHTTPHeaders: { Authorization: "Bearer local-dev" },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
  ],
});

export { BASE_URL, API_URL };

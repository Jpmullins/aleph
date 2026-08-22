/**
 * The web app's unit runner.
 *
 * apps/web had no way to run a test at all: its scripts were dev/build/preview/
 * typecheck/lint and its devDependencies contained no runner. The cost was not
 * hypothetical — WS-D3 wanted a test asserting `CopilotKitProvider` is mounted
 * with a `headers` object and had to ship a grep instead, and every UI defect on
 * the backlog was found by a human opening a browser.
 *
 * The vite config is IMPORTED rather than restated. A second copy of the `@/`
 * alias is the drift this repo keeps getting bitten by: the two would agree on
 * the day they were written and then one would move. `scripts/check-web-dead-
 * code.sh` reads the same alias out of the same file for the same reason.
 */
import { defineConfig, mergeConfig } from "vitest/config";

import viteConfig from "./vite.config";

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      // jsdom, not happy-dom: the subjects here touch `document.documentElement`
      // attributes, `localStorage`, `matchMedia` and `EventSource`, and jsdom is
      // the environment whose gaps are documented rather than surprising.
      environment: "jsdom",
      // Deliberately NOT `globals: true`. Every `describe`/`it`/`expect`/`vi` is
      // imported, so a test file reads the same as the app code beside it and
      // `tsc --noEmit` — which already type-checks everything under src/ — needs
      // no extra `types` entry to keep passing.
      globals: false,
      setupFiles: ["./vitest.setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      // `tests/playwright` is a real Playwright project with its own runner.
      // Without this, vitest collects its `.spec.ts` files, fails to resolve
      // `@playwright/test`, and reports a broken unit suite for a browser suite
      // that is not its job.
      exclude: ["**/node_modules/**", "dist/**", "../../tests/playwright/**"],
      restoreMocks: true,
      unstubEnvs: true,
      unstubGlobals: true,
    },
  }),
);

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
      /**
       * The floor, recorded rather than asserted.
       *
       * WS-UI-2 c6. `--coverage` reported `MISSING DEPENDENCY` — there was no
       * provider and no number, so "the web app has tests now" was a count of
       * tests and said nothing about how much of the app any of them touch.
       *
       * Measured 2026-08-22 over 216 tests in 23 files, with exactly the
       * include/exclude below:
       *   statements 38.14% (605/1586) · branches 24.51% (344/1403)
       *   functions  30.87% (159/515)  · lines    39.27% (562/1431)
       *
       * The thresholds sit ~1.5 points under each of those. Low enough that
       * deleting one small test does not fail the build, high enough that
       * deleting a suite does — a ratchet, not an aspiration. Raise them with
       * the coverage, never to match a number somebody wants to see.
       *
       * `enabled` is deliberately absent: coverage runs on `--coverage`, so
       * `pnpm test` stays fast and this block costs nothing until asked for.
       * Requires `@vitest/coverage-v8` — an OPTIONAL peer of vitest, so a tree
       * without it fails loudly at `--coverage` rather than silently reporting
       * nothing.
       */
      coverage: {
        provider: "v8",
        reporter: ["text-summary", "json-summary"],
        reportsDirectory: "./coverage",
        // The app, not the tests, and not the generated catalog — a generated
        // file's coverage measures the generator's test suite, not this one.
        include: ["src/**/*.{ts,tsx}"],
        exclude: ["src/**/*.test.{ts,tsx}", "src/a2ui/catalog.ts", "src/vite-env.d.ts"],
        thresholds: {
          statements: 36,
          branches: 23,
          functions: 29,
          lines: 37,
        },
      },
    },
  }),
);

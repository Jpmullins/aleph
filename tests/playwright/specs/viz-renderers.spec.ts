import { expect, test } from "@playwright/test";

import { createProject, deleteProject } from "./helpers";

/**
 * claim: viz-renderers — the ChartCard produces a real Vega-Lite <canvas>.
 *
 * Harvested from `audit/checks/e2e/viz-renderers.spec.ts`. The route it
 * intercepted no longer exists: there is no per-surface `/surfaces/wiki/stream`
 * any more. The whole reading region shares ONE multiplexed connection at
 * `/v1/projects/{id}/surfaces/stream?panes=…`, so that is what is fulfilled
 * here — which means this spec now also proves the multiplexed stream reaches
 * the right block, not only that vega-embed runs.
 *
 * The frames are the real wire shape, `seq` included: the provider drops any
 * frame whose seq is not strictly increasing, so a fixture without them would
 * pass for the wrong reason (or fail for one).
 */
const FRAMES = [
  { version: "v0.9", createSurface: { surfaceId: "wiki", catalogId: "aleph://v1" }, seq: 0 },
  {
    version: "v0.9",
    updateComponents: {
      surfaceId: "wiki",
      components: [
        {
          id: "root",
          component: "ChartCard",
          title: "E2E chart",
          chart_id: "e2e-chart",
          vega_lite_spec: {
            $schema: "https://vega.github.io/schema/vega-lite/v5.json",
            data: {
              values: [
                { x: 8, y: 0.18 },
                { x: 62, y: 0.34 },
                { x: 137, y: 0.43 },
                { x: 540, y: 0.57 },
              ],
            },
            mark: { type: "line", point: true },
            encoding: {
              x: { field: "x", type: "quantitative" },
              y: { field: "y", type: "quantitative" },
            },
          },
        },
      ],
    },
    seq: 1,
  },
];

const SSE_BODY = FRAMES.map((f, i) => `id: ${i}\ndata: ${JSON.stringify(f)}\n\n`).join("");

test("ChartCard renders a Vega-Lite canvas from the multiplexed surface stream", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "viz project");
  try {
    await page.route(/\/surfaces\/stream(\?.*)?$/, (route) =>
      route.fulfill({ status: 200, contentType: "text/event-stream", body: SSE_BODY }),
    );
    await page.goto(`/projects/${p.id}`);
    await expect(page.locator('[data-testid="chart-card-vega"] canvas')).toBeVisible({
      timeout: 30_000,
    });
  } finally {
    await deleteProject(request, p.id);
  }
});

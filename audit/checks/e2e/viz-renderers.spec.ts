import { expect, test } from "@playwright/test";

import { createProject, deleteProject } from "./helpers";

// claim: viz-renderers — the ChartCard produces a real Vega-Lite <canvas>.
// We fulfill the wiki surface SSE stream with a synthetic surface carrying a
// ChartCard, exercising the real renderer through the real A2UI stream path.
const SSE_BODY = [
  `data: ${JSON.stringify({ version: "v0.9", createSurface: { surfaceId: "wiki", catalogId: "aleph://v1" } })}`,
  "",
  `data: ${JSON.stringify({
    version: "v0.9",
    updateComponents: {
      surfaceId: "wiki",
      components: [
        { id: "root", component: "Column", children: ["ch-1"] },
        {
          id: "ch-1",
          component: "ChartCard",
          chart_id: "audit-chart",
          title: "Audit chart",
          vega_lite_spec: {
            $schema: "https://vega.github.io/schema/vega-lite/v5.json",
            data: { values: [
              { x: 8, y: 0.18 }, { x: 62, y: 0.34 }, { x: 137, y: 0.43 }, { x: 540, y: 0.57 },
            ] },
            mark: { type: "line", point: true },
            encoding: {
              x: { field: "x", type: "quantitative" },
              y: { field: "y", type: "quantitative" },
            },
          },
          children: [],
        },
      ],
    },
  })}`,
  "",
  "",
].join("\n");

test("ChartCard renders a real Vega-Lite canvas from the surface stream", async ({ page, request }) => {
  const p = await createProject(request, "viz project");
  try {
    await page.route(/\/surfaces\/wiki\/stream(\?.*)?$/, (route) =>
      route.fulfill({ status: 200, contentType: "text/event-stream", body: SSE_BODY }),
    );
    await page.goto(`/projects/${p.id}`);
    // The right panel defaults to the Wiki tab; the injected ChartCard should render.
    await expect(page.locator('[data-testid="chart-card-vega"] canvas')).toBeVisible({ timeout: 30_000 });
  } finally {
    await deleteProject(request, p.id);
  }
});

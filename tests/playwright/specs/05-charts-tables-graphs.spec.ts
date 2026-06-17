import { expect, test } from "@playwright/test";

import { API_URL } from "../playwright.config";
import { AUTH, cleanupTestProjects, createProject } from "./helpers";

/**
 * Synthetic A2UI surface served via a stub fetch override: lets us exercise
 * the real renderers for ChartCard / TableCard / MapCard / GraphCard /
 * HypothesisCard without needing a full upstream dataset pipeline.
 *
 * We POST a synthetic A2UI surface bundle to the page via the
 * `routeFulfill` mechanism — Playwright intercepts the surface request and
 * returns our crafted JSON. The renderer should produce real Vega-Lite /
 * SVG / MapLibre / sortable tables.
 */

const SYNTHETIC_WIKI_SURFACE = {
  tab: "wiki",
  surface: {
    id: "root",
    type: "WikiSurface",
    props: {},
    children: [
      {
        id: "ch-1",
        type: "ChartCard",
        props: {
          chart_id: "synthetic-chart-1",
          title: "CoT solve rate by model scale",
          vega_lite_spec: {
            $schema: "https://vega.github.io/schema/vega-lite/v5.json",
            data: {
              values: [
                { params_b: 8, solve_rate: 0.18 },
                { params_b: 62, solve_rate: 0.34 },
                { params_b: 137, solve_rate: 0.43 },
                { params_b: 540, solve_rate: 0.57 },
              ],
            },
            mark: { type: "line", point: true },
            encoding: {
              x: { field: "params_b", type: "quantitative", title: "Model size (B params)" },
              y: { field: "solve_rate", type: "quantitative", title: "GSM8K solve rate" },
            },
          },
        },
        children: [],
      },
      {
        id: "tbl-1",
        type: "TableCard",
        props: {
          title: "Benchmark results",
          columns: [
            { name: "model", label: "Model" },
            { name: "params_b", label: "Params (B)" },
            { name: "solve_rate", label: "Solve rate" },
          ],
          rows: [
            { model: "GPT-3", params_b: 175, solve_rate: 0.158 },
            { model: "PaLM", params_b: 540, solve_rate: 0.569 },
            { model: "Chinchilla", params_b: 70, solve_rate: 0.452 },
          ],
        },
        children: [],
      },
      {
        id: "gr-1",
        type: "GraphCard",
        props: {
          title: "Concept graph",
          nodes: [
            { id: "cot", label: "Chain-of-Thought" },
            { id: "sc", label: "Self-Consistency" },
            { id: "tot", label: "Tree-of-Thoughts" },
            { id: "gsm8k", label: "GSM8K" },
          ],
          edges: [
            { source: "cot", target: "sc", label: "extends" },
            { source: "sc", target: "tot", label: "extends" },
            { source: "cot", target: "gsm8k", label: "benchmark" },
          ],
        },
        children: [],
      },
      {
        id: "mp-1",
        type: "MapCard",
        props: {
          title: "Hypothetical lab locations",
          points: [
            { lat: 37.422, lon: -122.084, label: "Stanford" },
            { lat: 42.36, lon: -71.057, label: "MIT" },
            { lat: 51.751, lon: -1.255, label: "Oxford" },
          ],
        },
        children: [],
      },
    ],
  },
};

test.describe("Real visualization renderers", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    const p = await createProject(request, { title: "Visualization renderers test" });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test("Vega-Lite chart renders a real canvas", async ({ page }) => {
    await page.route(`**/v1/projects/${projectId}/surfaces/wiki`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SYNTHETIC_WIKI_SURFACE),
      });
    });

    await page.goto(`/projects/${projectId}`);
    await page.getByRole("button", { name: "Wiki" }).click();

    // Chart renders a <canvas> from vega-embed.
    await expect(page.locator('[data-testid="chart-card-vega"] canvas')).toBeVisible({
      timeout: 15_000,
    });
  });

  test("Table card sortable + filterable", async ({ page }) => {
    await page.route(`**/v1/projects/${projectId}/surfaces/wiki`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SYNTHETIC_WIKI_SURFACE),
      });
    });
    await page.goto(`/projects/${projectId}`);
    await page.getByRole("button", { name: "Wiki" }).click();

    // Filter input narrows rows.
    const tableScope = page.locator("table").first();
    await expect(tableScope).toBeVisible({ timeout: 10_000 });

    await page.getByPlaceholder("Filter…").first().fill("PaLM");
    await expect(tableScope.locator("tbody tr")).toHaveCount(1);

    await page.getByPlaceholder("Filter…").first().fill("");

    // Sort by clicking the header.
    await tableScope.locator("thead th:has-text('Params (B)')").click();
    const firstParamCell = tableScope.locator("tbody tr").first().locator("td").nth(1);
    await expect(firstParamCell).toContainText("70");
  });

  test("Graph card renders SVG nodes + edges", async ({ page }) => {
    await page.route(`**/v1/projects/${projectId}/surfaces/wiki`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SYNTHETIC_WIKI_SURFACE),
      });
    });
    await page.goto(`/projects/${projectId}`);
    await page.getByRole("button", { name: "Wiki" }).click();

    const svg = page.locator('[data-testid="graph-card"] svg');
    await expect(svg).toBeVisible({ timeout: 10_000 });
    await expect(svg.locator("circle")).toHaveCount(4);
    await expect(svg.locator("line")).toHaveCount(3);
  });

  test("Map card mounts MapLibre container", async ({ page }) => {
    await page.route(`**/v1/projects/${projectId}/surfaces/wiki`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(SYNTHETIC_WIKI_SURFACE),
      });
    });
    await page.goto(`/projects/${projectId}`);
    await page.getByRole("button", { name: "Wiki" }).click();

    await expect(page.locator('[data-testid="map-card"]')).toBeVisible({ timeout: 15_000 });
    // MapLibre creates a `.maplibregl-canvas` once it mounts.
    await expect(page.locator(".maplibregl-canvas").first()).toBeVisible({
      timeout: 20_000,
    });
  });
});

/**
 * The pane model and the multiplexed surface stream.
 *
 * Tabs assumed the app knows in advance what views exist. Panes do not, which
 * is what makes comparison — and later, agent-generated views — possible at
 * all. These assert the two properties that model depends on: panes coexist,
 * and they share ONE connection.
 */
import { expect, test } from "@playwright/test";

import { cleanupTestProjects, createProject } from "./helpers";

test.describe("@shell panes", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    projectId = (await createProject(request, { title: "Panes" })).id;
  });
  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test("the rail opens panes beside each other, not instead of", async ({ page }) => {
    await page.goto(`/projects/${projectId}`);
    await page.getByTestId("rail").waitFor();
    await page.getByTestId("rail-hypotheses").click();
    await page.getByTestId("rail-library").click();

    // All three render REAL content — not the loading placeholder, which is
    // what a broken stream would leave behind while still passing a
    // "container is visible" check.
    await expect(page.getByText("No wiki pages yet")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/No hypotheses yet/)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(/No sources yet/)).toBeVisible({ timeout: 15_000 });
  });

  test("re-opening a surface focuses it rather than duplicating it", async ({ page }) => {
    await page.goto(`/projects/${projectId}`);
    await page.getByTestId("rail").waitFor();
    await page.getByTestId("rail-notes").click();
    await page.getByTestId("rail-notes").click();
    await page.getByTestId("rail-notes").click();
    await expect(page.getByTestId("surface-notes")).toHaveCount(1);
  });

  test("all panes share ONE surface connection", async ({ page }) => {
    // Browsers cap ~6 concurrent EventSource per origin on HTTP/1.1 and Aleph
    // already opens two others, so a connection per pane breaks at four panes.
    await page.addInitScript(() => {
      const Original = window.EventSource;
      (window as { __live?: number }).__live = 0;
      class Counted extends Original {
        constructor(u: string | URL, i?: EventSourceInit) {
          super(u, i);
          if (String(u).includes("/surfaces/")) {
            (window as { __live?: number }).__live!++;
          }
        }
        close() {
          if (String(this.url).includes("/surfaces/")) {
            (window as { __live?: number }).__live!--;
          }
          super.close();
        }
      }
      (window as unknown as { EventSource: typeof EventSource }).EventSource =
        Counted as unknown as typeof EventSource;
    });

    await page.goto(`/projects/${projectId}`);
    await page.getByTestId("rail").waitFor();
    await page.getByTestId("rail-hypotheses").click();
    await page.getByTestId("rail-library").click();
    await expect(page.getByText(/No sources yet/)).toBeVisible({ timeout: 15_000 });

    const live = await page.evaluate(() => (window as { __live?: number }).__live);
    expect(live, "one surface stream should serve every pane").toBe(1);
  });

  test("closing a pane leaves the others", async ({ page }) => {
    await page.goto(`/projects/${projectId}`);
    await page.getByTestId("rail").waitFor();
    await page.getByTestId("rail-library").click();
    await expect(page.getByText(/No sources yet/)).toBeVisible({ timeout: 15_000 });
    await page.getByTestId("pane-close-library").click();
    await expect(page.getByTestId("surface-library")).toHaveCount(0);
    await expect(page.getByTestId("surface-wiki")).toBeVisible();
  });
});

import { expect, test } from "@playwright/test";

import {
  cleanupTestProjects,
  createProject,
  openProjectWorkspace,
} from "./helpers";

/**
 * Stage 0 — wiki link rendering, wikilink navigation, broken-link state, and
 * right-panel card scaling.
 *
 * The workspace shell + the wiki A2UI surface stream come from the live stack
 * (so the real renderer + SSE wiring is exercised), but the wiki *data*
 * endpoints are mocked so the link cases are deterministic regardless of what
 * the wiki agent compiled.
 */

const HUB_ID = "00000000-0000-4000-8000-000000000001";
const TARGET_ID = "00000000-0000-4000-8000-000000000002";

const HUB_BODY = [
  "# AI Distillation",
  "",
  "Distillation transfers knowledge; see [[Target Page]] for methods, and " +
    "[[Missing Page]] which has no page yet.",
  "",
  "Reference: [arXiv paper](https://arxiv.org/abs/1503.02531) and raw " +
    "https://example.com/spec.",
].join("\n");

function pageSummary(id: string, title: string, slug: string) {
  return {
    id,
    title,
    slug,
    page_kind: "topic",
    summary: `${title} summary`,
    is_stub: false,
    status: "approved",
    current_revision_id: `${id}-rev`,
    last_compiled_at: null,
  };
}

const PAGES = [
  pageSummary(HUB_ID, "AI Distillation", "ai-distillation"),
  { ...pageSummary(TARGET_ID, "Target Page", "target-page"), status: "draft" },
];

function detail(id: string, title: string, body: string, links: unknown[]) {
  return {
    page: pageSummary(id, title, title.toLowerCase().replace(/\s+/g, "-")),
    revision: { body_md: body, revision_no: 1, created_at: new Date(0).toISOString() },
    claims: [],
    wikilinks_out: links,
  };
}

test.describe("Stage 0 — wiki links, navigation, scaling", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    projectId = (await createProject(request, { title: "Wiki links + scaling" })).id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test.beforeEach(async ({ page }) => {
    // Mock the wiki data endpoints (list + detail) with deterministic content.
    await page.route(/\/wiki\/pages(\?.*)?$/, (route) =>
      route.fulfill({ json: PAGES }),
    );
    await page.route(new RegExp(`/wiki/pages/${HUB_ID}`), (route) =>
      route.fulfill({
        json: detail(HUB_ID, "AI Distillation", HUB_BODY, [
          { dst_title: "Target Page", dst_page_id: TARGET_ID, occurrences: 1 },
          { dst_title: "Missing Page", dst_page_id: null, occurrences: 1 },
        ]),
      }),
    );
    await page.route(new RegExp(`/wiki/pages/${TARGET_ID}`), (route) =>
      route.fulfill({
        json: detail(TARGET_ID, "Target Page", "# Target Page\n\nThe target page body.", []),
      }),
    );
  });

  test("external links, wikilink navigation, broken-link state", async ({ page }) => {
    await openProjectWorkspace(page, projectId);

    // Open the hub page from the wiki list.
    await page.getByTestId(`wiki-page-${HUB_ID}`).click();

    // External markdown link renders as a real anchor opening in a new tab.
    const ext = page.getByTestId("wiki-external-link").first();
    await expect(ext).toBeVisible();
    await expect(ext).toHaveAttribute("href", "https://arxiv.org/abs/1503.02531");
    await expect(ext).toHaveAttribute("target", "_blank");
    await expect(ext).toHaveAttribute("rel", /noreferrer/);

    // Bare URL is auto-linked too (so there are >= 2 external links).
    await expect(page.getByTestId("wiki-external-link")).toHaveCount(2);

    // Resolved inline wikilink is a navigable chip; broken one is distinct.
    await expect(page.getByTestId("wikilink-chip").filter({ hasText: "Target Page" })).toBeVisible();
    await expect(
      page.getByTestId("wikilink-broken").filter({ hasText: "Missing Page" }),
    ).toBeVisible();

    // "Links out" mirrors resolution: one navigable, one broken.
    await expect(page.getByTestId("wiki-linkout")).toHaveCount(1);
    await expect(page.getByTestId("wiki-linkout-broken")).toHaveCount(1);

    // Curation: the reader header shows the page's lifecycle status + provenance.
    await expect(page.getByTestId("wiki-status-badge").first()).toHaveText("approved");
    await expect(page.getByTestId("wiki-provenance")).toContainText("Revision 1");

    // Clicking an inline wikilink navigates the reader to the target page.
    await page.getByTestId("wikilink-chip").filter({ hasText: "Target Page" }).first().click();
    await expect(page.getByText("The target page body.")).toBeVisible();
  });

  test("draft pages are badged in the list; repair-links action fires", async ({ page }) => {
    // The repair endpoint is mocked; capture that the action calls it.
    let repairCalled = false;
    await page.route(/\/wiki\/aliases\/repair-links/, (route) => {
      repairCalled = true;
      return route.fulfill({ json: { repaired: 1 } });
    });

    await openProjectWorkspace(page, projectId);

    // The draft page carries a visible "draft" status badge in the list.
    await expect(page.getByTestId("wiki-status-badge").filter({ hasText: "draft" })).toBeVisible();

    // Open the hub (it has one broken link → repair affordance appears).
    await page.getByTestId(`wiki-page-${HUB_ID}`).click();
    const repairBtn = page.getByTestId("wiki-repair-links");
    await expect(repairBtn).toBeVisible();
    await expect(repairBtn).toContainText("Repair 1 broken link");
    await repairBtn.click();
    await expect.poll(() => repairCalled).toBe(true);
  });

  test("right panel fills its container (no fixed width)", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByTestId(`wiki-page-${HUB_ID}`).click();

    // The surface <aside> must fill its resizable parent panel rather than sit
    // at a fixed 28rem. Allow a 1px rounding tolerance.
    const fills = await page.evaluate(() => {
      const aside = document.querySelector("main ~ * aside, aside");
      if (!aside || !aside.parentElement) return false;
      return Math.abs(aside.clientWidth - aside.parentElement.clientWidth) <= 1;
    });
    expect(fills).toBe(true);
  });
});

import { expect, test } from "@playwright/test";

import { createProject, deleteProject, openWorkspace } from "./helpers";

// claim: wikilink-navigation — resolved wikilinks are navigable chips; broken
// links render a distinct state. Data endpoints are mocked (interceptable
// react-query fetches) so the cases are deterministic; the real renderer + shell
// are exercised live.
const HUB_ID = "00000000-0000-4000-8000-000000000001";
const TARGET_ID = "00000000-0000-4000-8000-000000000002";

const HUB_BODY = [
  "# Distillation",
  "",
  "See [[Target Page]] for methods, and [[Missing Page]] which has no page yet.",
].join("\n");

function summary(id: string, title: string, slug: string, status = "approved") {
  return {
    id, title, slug, page_kind: "topic", summary: `${title} summary`,
    is_stub: false, status, current_revision_id: `${id}-rev`, last_compiled_at: null,
  };
}
function detail(id: string, title: string, body: string, links: unknown[], status = "approved") {
  return {
    page: { ...summary(id, title, title.toLowerCase().replace(/\s+/g, "-"), status) },
    revision: { body_md: body, revision_no: 1, created_at: new Date(0).toISOString() },
    claims: [],
    wikilinks_out: links,
  };
}

test("resolved wikilink navigates; broken wikilink renders a distinct state", async ({ page, request }) => {
  const p = await createProject(request, "wikilink project");
  try {
    await page.route(/\/wiki\/pages(\?.*)?$/, (route) =>
      route.fulfill({ json: [summary(HUB_ID, "Distillation", "distillation"),
        { ...summary(TARGET_ID, "Target Page", "target-page", "draft") }] }),
    );
    await page.route(new RegExp(`/wiki/pages/${HUB_ID}`), (route) =>
      route.fulfill({ json: detail(HUB_ID, "Distillation", HUB_BODY, [
        { dst_title: "Target Page", dst_page_id: TARGET_ID, occurrences: 1 },
        { dst_title: "Missing Page", dst_page_id: null, occurrences: 1 },
      ]) }),
    );
    await page.route(new RegExp(`/wiki/pages/${TARGET_ID}(\\?.*)?$`), (route) =>
      route.fulfill({ json: detail(TARGET_ID, "Target Page", "# Target Page\n\nThe target page body.", [], "draft") }),
    );

    await openWorkspace(page, p.id);
    await page.getByTestId(`wiki-page-${HUB_ID}`).click();

    // Resolved chip visible; broken link distinct.
    await expect(page.getByTestId("wikilink-chip").filter({ hasText: "Target Page" })).toBeVisible();
    await expect(page.getByTestId("wikilink-broken").filter({ hasText: "Missing Page" })).toBeVisible();

    // Navigating a resolved chip loads the target page body.
    await page.getByTestId("wikilink-chip").filter({ hasText: "Target Page" }).first().click();
    await expect(page.getByText("The target page body.")).toBeVisible();
  } finally {
    await deleteProject(request, p.id);
  }
});

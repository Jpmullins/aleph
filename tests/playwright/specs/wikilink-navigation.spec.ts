import type { Route } from "@playwright/test";
import { expect, test } from "@playwright/test";

import { createProject, deleteProject, openWorkspace } from "./helpers";

/**
 * claim: wikilink-navigation — a resolved wikilink is a navigable chip and a
 * broken one renders a distinct, non-clickable state.
 *
 * Harvested from `audit/checks/e2e/wikilink-navigation.spec.ts` and REWRITTEN,
 * because the version in `audit/` could not pass and could not fail for the
 * right reason. It intercepted `GET /wiki/pages`, and `WikiSurface` has not
 * fetched since WP-4: it renders ONLY from the surface data model streamed over
 * `/surfaces/stream`, and opening a page is an A2UI action that re-keys the pane
 * so the server sends `/open` populated. Mocking the REST endpoints therefore
 * changed nothing and the spec timed out on markup that was never going to
 * appear.
 *
 * What is faked here is the stream and the action router — the two places a
 * deterministic broken link can be planted, since a real corpus has no
 * guaranteed one. The renderer, the reader, the wikilink resolution and the
 * pane model are all live.
 */
const HUB_ID = "00000000-0000-4000-8000-000000000001";
const TARGET_ID = "00000000-0000-4000-8000-000000000002";

const HUB_BODY = [
  "# Distillation",
  "",
  "See [[Target Page]] for methods, and [[Missing Page]] which has no page yet.",
].join("\n");

const TARGET_BODY = "# Target Page\n\nThe target page body.";

function summary(id: string, title: string, slug: string, status = "approved") {
  return {
    id,
    title,
    slug,
    page_kind: "topic",
    summary: `${title} summary`,
    is_stub: false,
    status,
    current_revision_id: `${id}-rev`,
    last_compiled_at: null,
  };
}

const PAGES = [
  summary(HUB_ID, "Distillation", "distillation"),
  summary(TARGET_ID, "Target Page", "target-page", "draft"),
];

function openPage(pageId: string) {
  const isHub = pageId === HUB_ID;
  return {
    page_id: pageId,
    title: isHub ? "Distillation" : "Target Page",
    status: isHub ? "approved" : "draft",
    is_stub: false,
    freshness: null,
    retracted: false,
    revision: {
      body_md: isHub ? HUB_BODY : TARGET_BODY,
      revision_no: 1,
      created_at: new Date(0).toISOString(),
    },
    claims: [],
    citations: [],
    wikilinks_out: isHub
      ? [
          { dst_title: "Target Page", dst_page_id: TARGET_ID, occurrences: 1 },
          { dst_title: "Missing Page", dst_page_id: null, occurrences: 1 },
        ]
      : [],
    html_url: null,
  };
}

/**
 * Serve the multiplexed stream for whatever pane set the client asked for.
 *
 * The pane id IS the wire `surfaceId` and it carries the page: `wiki` is the
 * index, `wiki:page_id=<uuid>` is a document. Reading the request's `panes`
 * parameter rather than replaying a fixed script is what makes this spec
 * exercise the real navigation path — the client only receives a document
 * because it asked for one.
 */
function serveStream(route: Route) {
  const panes = (new URL(route.request().url()).searchParams.get("panes") ?? "")
    .split(",")
    .filter(Boolean);
  const frames: unknown[] = [];
  for (const paneId of panes) {
    const match = /^wiki:page_id=(.+)$/.exec(paneId);
    frames.push({
      version: "v0.9",
      createSurface: { surfaceId: paneId, catalogId: "aleph://v1" },
    });
    frames.push({
      version: "v0.9",
      updateComponents: {
        surfaceId: paneId,
        components: [
          {
            id: "root",
            component: "WikiSurface",
            pages: { path: "/pages" },
            open: { path: "/open" },
            categories: { path: "/categories" },
            health: { path: "/health" },
          },
        ],
      },
    });
    frames.push({ version: "v0.9", updateDataModel: { surfaceId: paneId, path: "/pages", value: PAGES } });
    frames.push({
      version: "v0.9",
      updateDataModel: {
        surfaceId: paneId,
        path: "/open",
        value: match ? openPage(match[1]) : null,
      },
    });
    frames.push({ version: "v0.9", updateDataModel: { surfaceId: paneId, path: "/categories", value: [] } });
    frames.push({ version: "v0.9", updateDataModel: { surfaceId: paneId, path: "/health", value: {} } });
  }
  // `seq` is stamped last so it is strictly increasing across the whole
  // connection. The provider drops any frame that does not advance it, so a
  // fixture that reused a number would silently deliver half of itself.
  const body = frames
    .map((f, i) => `id: ${i}\ndata: ${JSON.stringify({ ...(f as object), seq: i })}\n\n`)
    .join("");
  return route.fulfill({ status: 200, contentType: "text/event-stream", body });
}

test("a resolved wikilink navigates; a broken one renders a distinct state", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "wikilink project");
  try {
    await page.route(/\/surfaces\/stream(\?.*)?$/, serveStream);
    // The action router is ledger-audited and real; what it would answer for a
    // fixture page is not, so the navigate target is supplied here. `tab` is
    // required as well as `page_id` — the card dispatcher only navigates when
    // the server names a surface.
    await page.route(/\/cards\/actions$/, async (route) => {
      const body = route.request().postDataJSON() as {
        params?: { target_id?: string; page_id?: string; slug?: string };
      };
      const pageId = body.params?.page_id ?? body.params?.target_id ?? HUB_ID;
      await route.fulfill({
        json: { result: { navigate: { tab: "Wiki", page_id: pageId } } },
      });
    });

    await openWorkspace(page, p.id);
    await page.getByTestId(`wiki-page-${HUB_ID}`).click();

    await expect(
      page.getByTestId("wikilink-chip").filter({ hasText: "Target Page" }).first(),
    ).toBeVisible();
    await expect(
      page.getByTestId("wikilink-broken").filter({ hasText: "Missing Page" }).first(),
    ).toBeVisible();

    await page.getByTestId("wikilink-chip").filter({ hasText: "Target Page" }).first().click();
    await expect(page.getByText("The target page body.").first()).toBeVisible();
  } finally {
    await deleteProject(request, p.id);
  }
});

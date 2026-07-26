/**
 * Clicking a wiki page must actually open it.
 *
 * This broke and nothing caught it. The click dispatched `open`, the API
 * returned `{navigate: {tab: "Wiki", page_id}}`, and the client called
 * `setOpenPageId(...)` — which stored the id in React state and stopped there.
 *
 * A pane's id IS its wire `surfaceId`; the stream subscribes with `?panes=<id>`
 * and the server reads the page out of that spec (`wiki:page_id=…`) to bind
 * `/open`. An id held only in state never reached the request, so `/open`
 * stayed null, `WikiSurface` kept rendering its index, and clicking a page did
 * visibly nothing — while every layer reported success: 200 from the action, a
 * correct navigate result, a state update. There was simply no path from that
 * state to the URL.
 *
 * Type-checking cannot see this. Every symbol exists and every signature
 * matches; the defect is a missing edge between two correct components.
 *
 * The SSE stream is faked here, deliberately and narrowly: the bug lives in the
 * URL the client *asks for*, so the assertion that matters is on the request,
 * and the response only has to be realistic enough to render. Everything
 * between the click and that request — the action POST, the navigate result,
 * the pane re-key, the resubscribe — is the real application.
 */
import { expect, test, type Page } from "@playwright/test";

import { cleanupTestProjects, createProject } from "./helpers";

const PAGE_ID = "019f9fb3-99e3-7e3b-99f9-add2916a5179";

const SUMMARY = {
  id: PAGE_ID,
  title: "AI Reasoning",
  slug: "ai-reasoning",
  page_kind: "overview",
  status: "draft",
  is_stub: false,
  current_revision_id: "019f9fb3-99e3-7e3b-99f9-add2916a5180",
  last_compiled_at: "2026-07-26T18:00:00Z",
  volatility: "warm",
  verified_at: "2026-07-26T18:00:00Z",
  freshness: 25,
  retracted: false,
};

const BODY_MARKER = "This wiki investigates AI Reasoning";

function frames(surfaceId: string, withOpen: boolean): string {
  const msgs: unknown[] = [
    { version: "v0.9", createSurface: { surfaceId, catalogId: "aleph://v1" }, seq: 0 },
    {
      version: "v0.9",
      updateComponents: {
        surfaceId,
        components: [
          {
            id: "root",
            component: "WikiSurface",
            pages: { path: "/pages" },
            open: { path: "/open" },
          },
        ],
      },
      seq: 1,
    },
    { version: "v0.9", updateDataModel: { surfaceId, path: "/pages", value: [SUMMARY] }, seq: 2 },
    {
      version: "v0.9",
      updateDataModel: {
        surfaceId,
        path: "/open",
        value: withOpen
          ? {
              page_id: PAGE_ID,
              title: SUMMARY.title,
              status: "draft",
              is_stub: false,
              freshness: 25,
              retracted: false,
              claims: [],
              citations: [],
              wikilinks_out: [],
              html_url: null,
              revision: { id: SUMMARY.current_revision_id, body_md: `# AI Reasoning\n\n${BODY_MARKER}.\n` },
            }
          : null,
      },
      seq: 3,
    },
  ];
  return msgs.map((m, i) => `id: ${i}\ndata: ${JSON.stringify(m)}\n\n`).join("");
}

/** Serve the surface stream, echoing whichever pane spec the client asked for. */
async function mockStream(page: Page, seen: string[]): Promise<void> {
  await page.route(/\/surfaces\/stream/, async (route) => {
    const panes = decodeURIComponent(new URL(route.request().url()).searchParams.get("panes") ?? "");
    seen.push(panes);
    // The surfaceId the server stamps IS the pane spec, verbatim.
    const surfaceId = panes.split(",")[0] ?? "wiki";
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: frames(surfaceId, surfaceId.includes("page_id=")),
    });
  });
}

test.describe("@shell opening a wiki page", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    projectId = (await createProject(request, { title: "Open page" })).id;
  });
  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test("a page click subscribes to that page and renders its body", async ({ page }) => {
    const seen: string[] = [];
    await mockStream(page, seen);
    await page.goto(`/projects/${projectId}`);

    await page.getByTestId(`wiki-page-${PAGE_ID}`).click({ timeout: 20_000 });

    await expect(page.getByTestId("wiki-back")).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText(BODY_MARKER)).toBeVisible({ timeout: 20_000 });

    expect(
      seen.some((s) => s.includes(`page_id=${PAGE_ID}`)),
      `no subscription carried the page id. Subscriptions: ${JSON.stringify(seen)}. ` +
        `The open id never reached the server, so /open stayed null.`,
    ).toBe(true);
  });

  test("opening a page does not spawn a second Wiki pane", async ({ page }) => {
    const seen: string[] = [];
    await mockStream(page, seen);
    await page.goto(`/projects/${projectId}`);
    await page.getByTestId(`wiki-page-${PAGE_ID}`).click({ timeout: 20_000 });
    await expect(page.getByTestId("wiki-back")).toBeVisible({ timeout: 20_000 });

    // `setActiveSurface("Wiki")` runs right after the pane is re-keyed. Matching
    // panes by exact id finds nothing called "wiki" and opens a duplicate, so
    // the same surface streams and renders twice.
    const last = seen[seen.length - 1] ?? "";
    const wikiPanes = last.split(",").filter((p) => p.startsWith("wiki")).length;
    expect(wikiPanes, `duplicate Wiki panes in "${last}"`).toBe(1);
  });

  test("back returns to the index", async ({ page }) => {
    const seen: string[] = [];
    await mockStream(page, seen);
    await page.goto(`/projects/${projectId}`);
    await page.getByTestId(`wiki-page-${PAGE_ID}`).click({ timeout: 20_000 });
    await page.getByTestId("wiki-back").click({ timeout: 20_000 });

    await expect(page.getByTestId("wiki-back")).toHaveCount(0);
    await expect(page.getByTestId(`wiki-page-${PAGE_ID}`)).toBeVisible({ timeout: 20_000 });
    expect(
      seen[seen.length - 1],
      "going back did not resubscribe without the page id",
    ).not.toContain("page_id=");
  });
});

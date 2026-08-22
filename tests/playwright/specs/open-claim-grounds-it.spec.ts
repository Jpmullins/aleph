import type { Route } from "@playwright/test";
import { expect, test } from "@playwright/test";

import { createProject, deleteProject, openWorkspace } from "./helpers";

/**
 * claim: "Open claim" opens the Grounding pane ON that claim.
 *
 * `ClaimCard` has emitted `open {target_kind: "claim"}` since it was written and
 * `_open` had no `claim` branch, so the response carried no `tab`, the card
 * dispatcher ignored it, and the button was decoration. The claim → citation →
 * chunk → char-span chain — the thing this whole project is built on — had no
 * route in from the browser at all.
 *
 * What is real here: the rail, the pane model, the card renderer, the click, the
 * POST, the pane id it mints, and the SUBSCRIPTION the client then opens. That
 * last one is the assertion that matters, because the pane id IS the wire
 * `surfaceId` and the server parses `claim_id` straight back out of it — a
 * navigate result that names the pane and drops the parameter opens an empty
 * Grounding surface, which renders identically to a claim with no evidence.
 *
 * What is faked: the stream (a real corpus has no guaranteed claim with a
 * guaranteed grounding) and the action router's answer. That answer's shape is
 * pinned against the real handler by
 * `tests/integration/test_surface_open_claim.py`, which drives `_open` through
 * `ActionRouter.dispatch` against Postgres.
 */
const CLAIM_ID = "00000000-0000-4000-8000-0000000000c1";
const GROUNDING_PANE = `grounding:claim_id=${CLAIM_ID}`;

/** Every `?panes=` the client has asked for, in order. */
const subscriptions: string[] = [];

function frames(paneId: string): unknown[] {
  if (paneId.startsWith("grounding")) {
    return [
      { version: "v0.9", createSurface: { surfaceId: paneId, catalogId: "aleph://v1" } },
      {
        version: "v0.9",
        updateComponents: {
          surfaceId: paneId,
          components: [
            {
              id: "root",
              component: "GroundingSurface",
              claim: { path: "/claim" },
              groundings: { path: "/groundings" },
            },
          ],
        },
      },
      {
        version: "v0.9",
        updateDataModel: {
          surfaceId: paneId,
          path: "/claim",
          value: {
            id: CLAIM_ID,
            text: "Chunks are written before the embed.",
            confidence: "supported",
            page_id: "00000000-0000-4000-8000-0000000000p1",
            page_title: "Retrieval",
          },
        },
      },
      {
        version: "v0.9",
        updateDataModel: {
          surfaceId: paneId,
          path: "/groundings",
          value: [
            {
              marker: "[c1]",
              source: { id: "s1", short_id: "S1", title: "A paper", url: null, retracted: false },
              chunks: [
                {
                  id: "ch1",
                  ordinal: 3,
                  text: "the verbatim passage the claim rests on",
                  char_start: 120,
                  char_end: 160,
                  section_path: "3.1",
                },
              ],
            },
          ],
        },
      },
    ];
  }
  // Any other pane renders one ClaimCard, which is the button under test.
  return [
    { version: "v0.9", createSurface: { surfaceId: paneId, catalogId: "aleph://v1" } },
    {
      version: "v0.9",
      updateComponents: {
        surfaceId: paneId,
        components: [
          {
            id: "root",
            component: "ClaimCard",
            claim_id: CLAIM_ID,
            text: "Chunks are written before the embed.",
            confidence: "supported",
          },
        ],
      },
    },
  ];
}

function serveStream(route: Route) {
  const panes = (new URL(route.request().url()).searchParams.get("panes") ?? "")
    .split(",")
    .filter(Boolean);
  subscriptions.push(panes.join(","));
  const all = panes.flatMap((paneId) => frames(paneId));
  const body = all
    .map((f, i) => `id: ${i}\ndata: ${JSON.stringify({ ...(f as object), seq: i })}\n\n`)
    .join("");
  return route.fulfill({ status: 200, contentType: "text/event-stream", body });
}

test("clicking Open claim subscribes the Grounding pane to that claim", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "open claim project");
  subscriptions.length = 0;
  try {
    await page.route(/\/surfaces\/stream(\?.*)?$/, serveStream);
    await page.route(/\/cards\/actions$/, async (route) => {
      const body = route.request().postDataJSON() as {
        action_kind?: string;
        params?: { target_id?: string; target_kind?: string };
      };
      // The request half is REAL — assert the card asked the right question.
      expect(body.action_kind).toBe("open");
      expect(body.params?.target_kind).toBe("claim");
      expect(body.params?.target_id).toBe(CLAIM_ID);
      await route.fulfill({
        json: {
          result: {
            navigate: {
              target_id: CLAIM_ID,
              target_kind: "claim",
              tab: "Grounding",
              params: { claim_id: CLAIM_ID },
            },
          },
        },
      });
    });

    await openWorkspace(page, p.id);
    await page.getByRole("button", { name: /Open claim/ }).first().click();

    // The pane exists on the board, keyed on the claim.
    await expect(page.locator(`[data-pane-kind="grounding"]`)).toHaveCount(1);
    // And the client actually ASKED the server for it, under the id that
    // carries the claim. Without the parameter the server builds an empty
    // grounding surface and nothing anywhere reports a problem.
    await expect
      .poll(() => subscriptions.some((s) => s.split(",").includes(GROUNDING_PANE)))
      .toBe(true);

    // The chain is on screen: the claim, and the passage it rests on.
    await expect(page.getByText("the verbatim passage the claim rests on")).toBeVisible();
  } finally {
    await deleteProject(request, p.id);
  }
});

import { expect, test } from "@playwright/test";

import { API_URL, AUTH, createProject, deleteProject, openWorkspace } from "./helpers";

/**
 * claim: settings panes tile beside content rather than covering it. WS-B1 #6.
 *
 * FAILED TODAY: settings was `<div className="fixed inset-0 z-30 flex">` — a
 * slide-over that covered the workspace, held in `ProjectWorkspace`'s own React
 * state, outside the pane model every other surface obeys. You could not read
 * a wiki page and change the model bound to synthesis at the same time, and no
 * plugin could contribute a section to it, because each section was a
 * hand-written React function.
 *
 * The assertions are the plan's: two Blocks on the Board with distinct
 * surfaceIds, and no fixed-position overlay. The second is the one that would
 * catch a "pane" implemented as an overlay wearing a Block's markup.
 */
test("settings is a pane kind the server serves", async ({ request }) => {
  const p = await createProject(request, "settings pane registry");
  try {
    const resp = await request.get(`${API_URL}/v1/projects/${p.id}/panes`, { headers: AUTH });
    expect(resp.ok()).toBe(true);
    const { panes } = (await resp.json()) as { panes: { id: string; launchable: boolean }[] };
    const ids = panes.map((k) => k.id);
    // The four that were drawers. `settings` is the criterion; the other three
    // are the sections WS-B1 had to port rather than lose, and a pane list that
    // has settings and not them is a port that dropped something.
    for (const id of ["settings", "logs", "notifications", "profile"]) {
      expect(ids, `pane kind ${id} is not served`).toContain(id);
    }
    expect(panes.find((k) => k.id === "settings")?.launchable).toBe(true);
  } finally {
    await deleteProject(request, p.id);
  }
});

test("opening Settings tiles a second block beside the wiki, covering nothing", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "settings tiles project");
  try {
    await openWorkspace(page, p.id);
    await expect(page.getByTestId("block")).toHaveCount(1);

    await page.getByTestId("rail-settings").click();
    await expect(page.getByTestId("block")).toHaveCount(2);

    // Distinct surfaces, not the same pane twice. The Board stamps the pane
    // kind on the block wrapper; two blocks of the same kind would mean the
    // rail duplicated a pane rather than opening a new one.
    const kinds = await page
      .locator("[data-pane-kind]")
      .evaluateAll((nodes) => nodes.map((n) => n.getAttribute("data-pane-kind")));
    expect(new Set(kinds).size).toBe(2);
    expect(kinds).toContain("Settings");

    // Nothing covers the workspace. Any `position: fixed` element that fills
    // the viewport is a drawer or a modal backdrop by another name; the wiki
    // block must still be hit-testable where it is drawn.
    const overlays = await page.evaluate(() => {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      return Array.from(document.querySelectorAll<HTMLElement>("body *"))
        .filter((el) => {
          const style = getComputedStyle(el);
          if (style.position !== "fixed") return false;
          if (style.display === "none" || style.visibility === "hidden") return false;
          if (style.pointerEvents === "none") return false;
          const r = el.getBoundingClientRect();
          return r.width >= vw * 0.9 && r.height >= vh * 0.9;
        })
        .map((el) => el.getAttribute("data-testid") ?? el.className.toString().slice(0, 80));
    });
    expect(overlays, "a full-viewport fixed overlay is on screen").toEqual([]);

    // The settings pane rendered its own content rather than an error frame.
    await expect(page.getByTestId("settings-surface")).toBeVisible();
  } finally {
    await deleteProject(request, p.id);
  }
});

test("the ledger pane shows the hash-chain verification the route had no caller for", async ({
  page,
  request,
}) => {
  // `GET /v1/projects/{id}/ledger/verify` existed with zero callers anywhere:
  // the append-only hash chain CLAUDE.md lists as a core invariant had no
  // interface at all. This is the consumer for that producer, which is the
  // house rule this repo names as its dominant defect class.
  const p = await createProject(request, "ledger pane project");
  try {
    await openWorkspace(page, p.id);
    await page.getByTestId("rail-logs").click();
    const chain = page.getByTestId("ledger-chain");
    await expect(chain).toBeVisible();
    // Creating a project writes ledger rows, so the chain is non-empty AND
    // verifies. Asserting only "the element exists" would pass on a pane that
    // rendered the element with nothing in it.
    await expect(chain).toHaveAttribute("data-ok", "true");
    await expect(chain).toContainText(/\d+ events/);
  } finally {
    await deleteProject(request, p.id);
  }
});

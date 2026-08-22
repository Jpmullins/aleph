import { expect, test } from "@playwright/test";

import { AUTH, API_URL, createProject, deleteProject, openWorkspace } from "./helpers";

/**
 * claim: workspace-three-panel-shell — rail · reading region · assistant dock.
 *
 * Harvested from `audit/checks/e2e/workspace-three-panel-shell.spec.ts` and
 * REWRITTEN, because what it asserted no longer exists. It required five
 * surface tabs — Wiki, Library, Notes, Hypotheses, Briefs — above the right
 * panel. That tab bar is gone: the surfaces come from
 * `GET /v1/projects/{id}/panes` and the reading region is a canvas of blocks.
 * A spec asserting five compiled-in names would have to be updated by hand
 * every time a plugin is installed, which is the opposite of what the registry
 * is for.
 *
 * So the assertion is now against the SERVER's answer. That is the version that
 * can fail for a real reason: add a pane kind to the registry and forget the
 * client, or mark one unlaunchable, and this goes red.
 */
test("the workspace shows the rail, the board and the assistant dock", async ({ page, request }) => {
  const p = await createProject(request, "shell project");
  try {
    await openWorkspace(page, p.id);
    await expect(page.getByTestId("rail")).toBeVisible();
    await expect(page.getByTestId("context-bar")).toBeVisible();
    await expect(page.getByTestId("board")).toBeVisible();
    await expect(page.getByTestId("assistant-dock")).toBeVisible();
  } finally {
    await deleteProject(request, p.id);
  }
});

test("the rail offers exactly the launchable panes the server declares", async ({ page, request }) => {
  const p = await createProject(request, "rail registry project");
  try {
    const resp = await request.get(`${API_URL}/v1/projects/${p.id}/panes`, { headers: AUTH });
    expect(resp.ok()).toBe(true);
    const { panes } = (await resp.json()) as {
      panes: { id: string; title: string; launchable: boolean }[];
    };
    const launchable = panes.filter((k) => k.launchable);
    expect(launchable.length).toBeGreaterThan(0);

    await openWorkspace(page, p.id);
    // Addressed by ID, which is what the rail stamps now. The lower-cased
    // TITLE agreed with the id for every core pane and for none of the ones
    // this registry exists to make possible.
    for (const kind of launchable) {
      await expect(page.getByTestId(`rail-${kind.id}`)).toBeVisible();
    }
    for (const kind of panes.filter((k) => !k.launchable)) {
      await expect(page.getByTestId(`rail-${kind.id}`)).toHaveCount(0);
    }
  } finally {
    await deleteProject(request, p.id);
  }
});

test("opening a second pane from the rail puts a second block on the board", async ({ page, request }) => {
  // The rail is a LAUNCHER, not a switcher. That distinction is the whole
  // difference between tabs and a workspace you can compare two things in, and
  // it is invisible in a screenshot of either one.
  const p = await createProject(request, "two block project");
  try {
    await openWorkspace(page, p.id);
    await expect(page.getByTestId("block")).toHaveCount(1);
    await page.getByTestId("rail-notes").click();
    await expect(page.getByTestId("block")).toHaveCount(2);
  } finally {
    await deleteProject(request, p.id);
  }
});

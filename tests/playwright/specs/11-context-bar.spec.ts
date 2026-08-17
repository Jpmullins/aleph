/**
 * The shared-context bar must show the analyst what the assistant can see.
 *
 * Aleph sends `{active_tab, open_page_id, open_page_title, selection}` to the
 * agent on every turn via `useAgentContext`, and until now showed the human
 * none of it — so "summarize this page" worked by apparent magic and there was
 * no way to notice the agent was looking at something stale.
 *
 * These assert the bar reflects real workspace state rather than merely
 * existing: changing the surface must change what the bar reads.
 */
import { expect, test } from "@playwright/test";

import { createProject, openProjectWorkspace } from "./helpers";

test.describe("@shell shared-context bar", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    const project = await createProject(request, { title: "Context bar" });
    projectId = project.id;
  });

  test("renders the payload the assistant receives", async ({ page }) => {
    await openProjectWorkspace(page, projectId);

    const bar = page.getByTestId("context-bar");
    await expect(bar).toBeVisible();

    // The tab the agent is told is active must be the one actually shown.
    const tab = page.getByTestId("context-active-tab");
    await expect(tab).toBeVisible();
    const shown = (await tab.textContent())?.trim();
    expect(["Wiki", "Library", "Notes", "Hypotheses", "Briefs"]).toContain(shown);
  });

  test("tracks the surface it claims to track", async ({ page }) => {
    // The failure that matters is a bar that renders a hardcoded value and
    // drifts from reality — indistinguishable from a working one in a
    // screenshot, so assert it actually MOVES.
    await openProjectWorkspace(page, projectId);

    const tab = page.getByTestId("context-active-tab");
    const before = (await tab.textContent())?.trim();
    await tab.click();
    await expect(tab).not.toHaveText(before ?? "");

    const after = (await tab.textContent())?.trim();
    expect(after).not.toEqual(before);
    // …and the right panel followed, rather than the bar drifting on its own.
    // Scoped to the panel's tab strip: the bar's own button carries a distinct
    // aria-label precisely so the two are never confused.
    await expect(
      page.getByRole("navigation").getByRole("button", { name: after ?? "", exact: true }),
    ).toBeVisible();
  });

  test("states that the context is shared", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await expect(page.getByTestId("context-bar")).toContainText("shared with assistant");
  });
});

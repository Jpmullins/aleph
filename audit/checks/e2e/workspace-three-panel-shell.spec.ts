import { expect, test } from "@playwright/test";

import { createProject, deleteProject, openWorkspace } from "./helpers";

// claim: workspace-three-panel-shell — left rail + center chat + right 5 tabs.
test("workspace shows the 3-panel shell with all 5 surface tabs", async ({ page, request }) => {
  const p = await createProject(request, "shell project");
  try {
    await openWorkspace(page, p.id);
    // Left rail
    await expect(page.getByText("Sessions").first()).toBeVisible();
    // Right panel: the 5 A2UI surface tabs
    for (const tab of ["Wiki", "Library", "Notes", "Hypotheses", "Briefs"]) {
      await expect(page.getByRole("button", { name: tab, exact: true }).first()).toBeVisible();
    }
  } finally {
    await deleteProject(request, p.id);
  }
});

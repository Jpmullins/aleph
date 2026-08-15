import { expect, test } from "@playwright/test";

import { createProject, deleteProject } from "./helpers";

// claim: projects-list — the landing page lists projects (each openable).
test("projects list renders real projects with open affordances", async ({ page, request }) => {
  const p = await createProject(request, "listed project");
  try {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
    const card = page.getByTestId(`project-open-${p.id}`);
    await expect(card).toBeVisible({ timeout: 20_000 });
    await expect(card).toContainText("listed project");
  } finally {
    await deleteProject(request, p.id);
  }
});

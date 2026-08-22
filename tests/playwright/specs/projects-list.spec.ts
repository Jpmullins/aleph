import { expect, test } from "@playwright/test";

import { createProject, deleteProject } from "./helpers";

/**
 * claim: projects-list — the landing page lists real projects, each openable.
 *
 * Harvested from `audit/checks/e2e/projects-list.spec.ts`. The project is
 * created through the API rather than the UI so this spec fails for exactly one
 * reason: the list did not render what the API returned.
 */
test("the landing page lists real projects with an open affordance", async ({ page, request }) => {
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

test("opening a project from the list lands in its workspace", async ({ page, request }) => {
  const p = await createProject(request, "openable project");
  try {
    await page.goto("/");
    await page.getByTestId(`project-open-${p.id}`).click();
    await expect(page).toHaveURL(new RegExp(`/projects/${p.id}`));
    await expect(page.getByTestId("rail")).toBeVisible();
  } finally {
    await deleteProject(request, p.id);
  }
});

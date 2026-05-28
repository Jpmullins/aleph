import { expect, test } from "@playwright/test";

import { API_URL } from "../playwright.config";
import { AUTH, cleanupAllProjects, createProject } from "./helpers";

test.describe("Project lifecycle", () => {
  test.beforeAll(async ({ request }) => {
    await cleanupAllProjects(request);
  });

  test("list shows empty state when no projects exist", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
    await expect(page.getByText(/No projects yet/i)).toBeVisible();
  });

  test("create + open a new project via UI", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "New project" }).click();
    await page.getByLabel("Title").fill("Playwright Test Project");
    await page.getByLabel("Description").fill("Created by Playwright");
    await page.getByRole("button", { name: "Create" }).click();

    await expect(page).toHaveURL(/\/projects\/[0-9a-f-]+/);
    await expect(page.getByRole("heading", { name: "Playwright Test Project" })).toBeVisible();
    await expect(page.getByRole("complementary").filter({ hasText: "Sessions" })).toBeVisible();
  });

  test("delete a project removes it from the list", async ({ page, request }) => {
    const p = await createProject(request, { title: "Stale project to delete" });
    await page.goto("/");

    const card = page.getByTestId(`project-open-${p.id}`);
    await expect(card).toBeVisible();
    await expect(card).toContainText("Stale project to delete");

    page.once("dialog", (d) => d.accept());
    await page.getByTestId(`project-delete-${p.id}`).click();

    await expect(card).not.toBeVisible({ timeout: 10_000 });

    // Verify backend status flipped to "deleted".
    const resp = await request.get(`${API_URL}/v1/projects/${p.id}`, { headers: AUTH });
    const project = await resp.json();
    expect(project.status).toBe("deleted");
  });

  test.afterAll(async ({ request }) => {
    await cleanupAllProjects(request);
  });
});

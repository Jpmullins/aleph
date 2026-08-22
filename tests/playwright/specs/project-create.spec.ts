import { expect, test } from "@playwright/test";

import { cleanupE2EProjects } from "./helpers";

/**
 * claim: project-create — a project created from the UI opens its workspace.
 *
 * Harvested from `audit/checks/e2e/project-create.spec.ts`. The final assertion
 * changed: the old one waited for the text "Sessions", which belonged to a left
 * panel deleted with the shell rebuild. The workspace is now rail · board ·
 * dock, so the rail is what proves the workspace mounted.
 */
test.afterAll(async ({ request }) => {
  await cleanupE2EProjects(request);
});

test("creating a project from the UI opens its workspace", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New project" }).click();
  // Scoped to the modal form so the "Create" submit button is unambiguous —
  // strict-mode locator resolution fails on two buttons with the same name.
  const form = page.locator("form").filter({ has: page.getByLabel("Title") });
  await form.getByLabel("Title").fill("[e2e] created via UI");
  await form.getByLabel("Description").fill("Created by the browser suite");
  await form.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]+/, { timeout: 20_000 });
  await expect(page.getByTestId("rail")).toBeVisible();
  await expect(page.getByTestId("board")).toBeVisible();
});

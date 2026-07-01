import { expect, test } from "@playwright/test";

import { cleanupAuditProjects } from "./helpers";

// claim: project-create — create a project from the UI and land in its workspace.
test.afterAll(async ({ request }) => {
  await cleanupAuditProjects(request);
});

test("create a project via the UI opens its 3-panel workspace", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "New project" }).click();
  // Scope to the modal form so the "Create" submit button is unambiguous.
  const form = page.locator("form").filter({ has: page.getByLabel("Title") });
  await form.getByLabel("Title").fill("[audit-e2e] created via UI");
  await form.getByLabel("Description").fill("Created by the audit harness");
  await form.getByRole("button", { name: "Create", exact: true }).click();

  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]+/, { timeout: 20_000 });
  await expect(page.getByText("Sessions").first()).toBeVisible();
});

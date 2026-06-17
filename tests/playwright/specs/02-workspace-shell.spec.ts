import { expect, test } from "@playwright/test";

import {
  cleanupTestProjects,
  createProject,
  createSession,
  openProjectWorkspace,
  sendChat,
} from "./helpers";

test.describe("Workspace shell — panels, sessions, drawers, chat", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    const p = await createProject(request, { title: "Workspace test" });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test("3-panel shell shows left panel, center main, right tabs", async ({ page }) => {
    await openProjectWorkspace(page, projectId);

    // (W6 moved cost out of the shell into the Settings/Profile drawers —
    // asserted in the Settings drawer test below.)
    await expect(page.getByRole("button", { name: "← Projects" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Workspace test" })).toBeVisible();
    await expect(page.getByText("Sessions", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "+ Upload source" })).toBeVisible();
    for (const tab of ["Wiki", "Artifacts", "Notes", "Hypotheses", "Briefs"]) {
      await expect(page.getByRole("button", { name: tab })).toBeVisible();
    }
    await expect(page.getByTestId("activity-card-toggle")).toBeVisible();

    // Resize handles exist as ARIA separators.
    const separators = page.getByRole("separator");
    await expect(separators).toHaveCount(2);
  });

  test("create a new session via + New", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await createSession(page);
    await expect(page.getByTestId("copilot-chat-textarea")).toBeEnabled();
  });

  test("Shift+Enter inserts a newline (does not submit)", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await createSession(page);

    const composer = page.getByTestId("copilot-chat-textarea");
    await composer.fill("line one");
    await composer.press("Shift+Enter");
    await composer.pressSequentially("line two");
    await expect(composer).toHaveValue("line one\nline two");
    // The send button stays available — submission has NOT fired.
    await expect(page.getByTestId("copilot-send-button")).toBeEnabled();
  });

  test("Plain Enter submits the chat (user bubble appears, composer clears)", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await createSession(page);

    const composer = page.getByTestId("copilot-chat-textarea");
    await composer.fill("Single-line question?");
    await composer.press("Enter");

    // Wait for the user bubble — that's the strongest signal the
    // mutation actually ran. Composer clearing is observed next.
    await expect(page.getByTestId("copilot-user-message").first()).toContainText(
      "Single-line question?",
      { timeout: 30_000 },
    );
    await expect(composer).toHaveValue("", { timeout: 5_000 });
  });

  test("Settings drawer renders real project metadata", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "⚙" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Project" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Budget" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Members" })).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).not.toBeVisible();
  });

  test("Logs drawer shows action ledger events", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "🗒" }).click();
    await expect(page.getByRole("heading", { name: "Action ledger" })).toBeVisible();
    // We expect at least project.create from the beforeAll setup.
    await expect(page.getByText("project.create").first()).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();
  });

  test("Notifications drawer shows agent-run sections", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "Notifications" }).click();
    await expect(page.getByRole("heading", { name: "Notifications" })).toBeVisible();
    // The "Running" section is always present (even if empty).
    await expect(page.getByText(/Running \(\d+\)/)).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();
  });

  test("Profile drawer shows current user", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "●" }).click();
    await expect(page.getByRole("heading", { name: "Profile" })).toBeVisible();
    await expect(page.getByText("dev@aleph.local")).toBeVisible();
    await page.getByRole("button", { name: "Close" }).click();
  });
});

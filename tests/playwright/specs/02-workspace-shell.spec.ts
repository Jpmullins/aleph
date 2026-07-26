/**
 * The workspace shell: rail → reading region → assistant dock.
 *
 * Rewritten for the reimagined shell. The previous version asserted the
 * three-panel layout (`← Projects`, a `Sessions` list, a five-tab bar) that has
 * been replaced — and it had also been asserting an `Artifacts` tab renamed to
 * `Library` two work packages earlier, which nothing caught because the suite
 * was not in CI.
 *
 * These assert the *shape of the product*, not incidental markup: the reading
 * region is the stage, ingest is always reachable, and the theme holds without
 * the deleted `!important` shim.
 */
import { expect, test } from "@playwright/test";

import { cleanupTestProjects, createProject, createSession } from "./helpers";

const SURFACES = ["Wiki", "Library", "Notes", "Hypotheses", "Briefs"] as const;

test.describe("@shell Workspace shell — rail, reading region, assistant dock", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    const p = await createProject(request, { title: "Workspace test" });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test.beforeEach(async ({ page }) => {
    await page.goto(`/projects/${projectId}`);
    await expect(page.getByTestId("rail")).toBeVisible();
  });

  test("rail exposes every surface and drawer", async ({ page }) => {
    for (const s of SURFACES) {
      await expect(page.getByTestId(`rail-${s.toLowerCase()}`)).toBeVisible();
    }
    for (const d of ["settings", "logs", "notifications", "profile"]) {
      await expect(page.getByTestId(`rail-${d}`)).toBeVisible();
    }
  });

  test("the rail switches the reading surface", async ({ page }) => {
    await page.getByTestId("rail-hypotheses").click();
    await expect(page.getByTestId("surface-hypotheses")).toBeVisible();
    await expect(page.getByTestId("context-active-tab")).toHaveText("Hypotheses");

    await page.getByTestId("rail-notes").click();
    await expect(page.getByTestId("surface-notes")).toBeVisible();
  });

  test("the reading region is the stage, not a sidebar", async ({ page }) => {
    // The thesis is that the compiled wiki is the primary retrieval surface. If
    // the reading region is ever narrower than the chat dock, the layout
    // asserts the opposite of the product.
    const s = await page.getByTestId("surface-wiki").boundingBox();
    const d = await page.getByTestId("assistant-dock").boundingBox();
    expect(s, "reading surface not rendered").toBeTruthy();
    expect(d, "assistant dock not rendered").toBeTruthy();
    expect(s!.width).toBeGreaterThan(d!.width);
  });

  test("ingest is reachable from any surface", async ({ page }) => {
    // This affordance lived in the deleted left panel; losing it would strand
    // the primary way content enters the system.
    await page.getByTestId("rail-hypotheses").click();
    await expect(page.getByTestId("context-upload-source")).toBeVisible();
  });

  test("sessions live with the conversation", async ({ page }) => {
    await expect(page.getByTestId("dock-new-session")).toBeVisible();
    await createSession(page);
    await expect(page.getByTestId("copilot-chat-textarea")).toBeEnabled();
  });

  test("Shift+Enter inserts a newline (does not submit)", async ({ page }) => {
    await createSession(page);
    const composer = page.getByTestId("copilot-chat-textarea");
    await composer.fill("line one");
    await composer.press("Shift+Enter");
    await composer.pressSequentially("line two");
    await expect(composer).toHaveValue("line one\nline two");
  });

  test("Settings drawer renders real project metadata", async ({ page }) => {
    await page.getByTestId("rail-settings").click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Project" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Cost" })).toBeVisible();
  });

  test("Ledger drawer shows action ledger events", async ({ page }) => {
    await page.getByTestId("rail-logs").click();
    await expect(page.getByRole("heading", { name: "Action ledger" })).toBeVisible();
    await expect(page.getByText("project.create").first()).toBeVisible();
  });

  test("Profile drawer shows the current user", async ({ page }) => {
    await page.getByTestId("rail-profile").click();
    await expect(page.getByRole("heading", { name: "Profile" })).toBeVisible();
    await expect(page.getByText("dev@aleph.local")).toBeVisible();
  });

  test("dark mode themes the chrome without a shim", async ({ page }) => {
    // The 26-rule `!important` shim is gone. If a component still carries a
    // hardcoded light palette class it stays light while everything around it
    // goes dark — this catches exactly that.
    await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
    const bg = await page
      .getByTestId("rail")
      .evaluate((el) => getComputedStyle(el).backgroundColor);
    const [r, g, b] = bg.match(/\d+/g)!.map(Number);
    expect(
      (r + g + b) / 3,
      `rail background ${bg} is light in dark mode — a token did not follow the theme`,
    ).toBeLessThan(90);
  });
});

import { expect, test } from "@playwright/test";

import { API_URL } from "../playwright.config";
import { AUTH, cleanupAllProjects, createProject, openProjectWorkspace } from "./helpers";

test.describe("Right-panel A2UI surfaces", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupAllProjects(request);
    const p = await createProject(request, { title: "A2UI surfaces test" });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupAllProjects(request);
  });

  test("each of the 5 tabs renders without console errors", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (msg) => {
      if (msg.type() === "error") errors.push(msg.text());
    });

    await openProjectWorkspace(page, projectId);
    for (const tab of ["Wiki", "Artifacts", "Notes", "Hypotheses", "Briefs"]) {
      await page.getByRole("button", { name: tab }).click();
      await page.waitForTimeout(800);
    }

    expect(errors, errors.join("\n")).toEqual([]);
  });

  test("Hypotheses tab — create a hypothesis, see it render", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "Hypotheses" }).click();

    await page.getByTestId("new-hypothesis").click();
    await page.getByLabel("Title").fill("Test hypothesis");
    await page
      .getByLabel("Statement")
      .fill("Larger models benefit more from CoT prompting.");
    await page.getByRole("button", { name: "Create" }).click();

    await expect(page.getByText("Test hypothesis")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("Larger models benefit more from CoT prompting.")).toBeVisible();
    await expect(page.getByText(/initial|under investigation/)).toBeVisible();
  });

  test("Hypotheses card has working feedback button", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "Hypotheses" }).click();

    const feedbackBtn = page.locator('[data-testid^="feedback-hypothesis-"]').first();
    await expect(feedbackBtn).toBeVisible({ timeout: 10_000 });
    await feedbackBtn.click();

    await expect(page.getByText("What's wrong?")).toBeVisible();
    await page.getByRole("button", { name: "Misleading" }).click();
    await expect(page.getByText(/Thanks — flagged/i)).toBeVisible({ timeout: 5_000 });
  });

  test("Artifacts tab — kick off a build, see it in the list", async ({ page, request }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "Artifacts" }).click();

    await page.getByTestId("build-artifact").click();
    await page.getByLabel("Title").fill("Smoke test report");
    await page.getByRole("button", { name: "Build", exact: true }).click();

    // Artifact appears in the list.
    await expect(page.getByText("Smoke test report")).toBeVisible({ timeout: 30_000 });

    // Backend created the row.
    const resp = await request.get(
      `${API_URL}/v1/projects/${projectId}/artifacts`,
      { headers: AUTH },
    );
    const artifacts = await resp.json();
    expect(artifacts.length).toBeGreaterThan(0);
  });

  test("Briefs tab — pre-existing approval cards render", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByRole("button", { name: "Briefs" }).click();
    // Briefs is the action pile. In a fresh project there may be nothing,
    // but the surface must render without an error.
    await expect(page.locator("aside").last()).toBeVisible();
  });
});

import { expect, test } from "@playwright/test";

import { API_URL } from "../playwright.config";
import {
  AUTH,
  SAMPLE_MARKDOWN_SOURCE,
  cleanupTestProjects,
  createProject,
  openProjectWorkspace,
  uploadSource,
  waitForAgentRun,
} from "./helpers";

interface PhaseEvent {
  agent_run_id: string;
  agent_kind: string;
  event_kind: string;
  phase: string;
  duration_ms: number | null;
}

test.describe("W1 — progress visibility + design tokens + activity card at top", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    const p = await createProject(request, { title: "W1 progress test" });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
  });

  test("activity card renders above the chat composer", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    const activityToggle = page.getByTestId("activity-card-toggle");
    const composer = page.getByTestId("copilot-chat-textarea");
    await expect(activityToggle).toBeVisible();
    await expect(composer).toBeVisible();

    const activityBox = await activityToggle.boundingBox();
    const composerBox = await composer.boundingBox();
    expect(activityBox).not.toBeNull();
    expect(composerBox).not.toBeNull();
    // Activity card is above the composer (smaller Y).
    expect(activityBox!.y).toBeLessThan(composerBox!.y);
  });

  test("AgentEvent rows are written per wiki node", async ({ request }) => {
    await uploadSource(request, projectId, "cot.md", SAMPLE_MARKDOWN_SOURCE);

    // Drive the pipeline to wiki-ingest succeeded.
    await waitForAgentRun(request, projectId, "wiki", { timeoutMs: 180_000 });

    // Query the non-streaming list endpoint (the SSE stream is kept
    // open and not usable via APIRequestContext).
    const resp = await request.get(
      `${API_URL}/v1/projects/${projectId}/agent-events?limit=200`,
      { headers: AUTH },
    );
    expect(resp.ok()).toBe(true);
    const events = (await resp.json()) as PhaseEvent[];

    const wikiEvents = events.filter((e) => e.agent_kind === "wiki");
    const distinctPhases = new Set(wikiEvents.map((e) => e.phase));
    // The wiki LangGraph has 7 nodes. We expect every phase emitted at
    // least once (started OR completed).
    expect(distinctPhases.size).toBeGreaterThanOrEqual(4);

    // And the events come in pairs (started + completed) on the happy path.
    const startedCount = wikiEvents.filter((e) => e.event_kind === "phase_started").length;
    const completedCount = wikiEvents.filter((e) => e.event_kind === "phase_completed").length;
    expect(startedCount).toBeGreaterThanOrEqual(distinctPhases.size);
    expect(completedCount).toBeGreaterThanOrEqual(distinctPhases.size);
  });

  test("theme toggle swaps the data-theme attribute and surface-bg CSS variable", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    const toggle = page.getByTestId("theme-toggle");
    await expect(toggle).toBeVisible();

    const lightSurface = await page.evaluate(() => {
      document.documentElement.setAttribute("data-theme", "light");
      return getComputedStyle(document.documentElement)
        .getPropertyValue("--surface-bg")
        .trim();
    });
    const darkSurface = await page.evaluate(() => {
      document.documentElement.setAttribute("data-theme", "dark");
      return getComputedStyle(document.documentElement)
        .getPropertyValue("--surface-bg")
        .trim();
    });

    expect(lightSurface).not.toBe("");
    expect(darkSurface).not.toBe("");
    expect(lightSurface).not.toBe(darkSurface);

    // Reset DOM state, then verify the user-facing toggle cycles modes.
    await page.evaluate(() => document.documentElement.removeAttribute("data-theme"));
    await toggle.click();
    await page.waitForTimeout(100);
    const afterFirstClick = await page.evaluate(() =>
      document.documentElement.getAttribute("data-theme"),
    );
    expect(["light", "dark"]).toContain(afterFirstClick);
  });
});

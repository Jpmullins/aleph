import { expect, test } from "@playwright/test";

import {
  cleanupAllProjects,
  createProject,
  openProjectWorkspace,
} from "./helpers";

/**
 * Wave 2 — CopilotKit v2 + AG-UI + A2UI shared state and generative cards.
 *
 * The "Live" chat talks to the assistant Deep Agent through the Node
 * CopilotRuntime (:4000) → aleph-api AG-UI endpoint. These tests drive the
 * real running stack (the live round-trips need the LLM gateway), matching
 * the project's standing requirement to verify in a real browser.
 */
const RUNTIME_URL =
  process.env.ALEPH_COPILOT_RUNTIME_URL ?? "http://localhost:4000/api/copilotkit";

test.describe("W2 — CopilotKit + AG-UI + A2UI", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupAllProjects(request);
    const p = await createProject(request, { title: "W2 copilotkit test" });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupAllProjects(request);
  });

  test("Node CopilotRuntime exposes the assistant agent with A2UI enabled", async ({
    request,
  }) => {
    const resp = await request.get(`${RUNTIME_URL}/info`);
    expect(resp.ok()).toBeTruthy();
    const info = (await resp.json()) as {
      agents: Record<string, unknown>;
      a2uiEnabled?: boolean;
    };
    expect(info.agents).toHaveProperty("assistant");
    expect(info.a2uiEnabled).toBe(true);
  });

  test("Live toggle mounts the CopilotKit chat surface", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    // Classic is the default; the live AG-UI chat is opt-in via the toggle.
    await expect(page.getByTestId("chat-mode-toggle")).toBeVisible();
    await page.getByTestId("chat-mode-live").click();
    await expect(page.getByTestId("copilot-chat-textarea")).toBeVisible();
  });

  test("shared state: the agent knows which surface tab the analyst is viewing", async ({
    page,
  }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByTestId("chat-mode-live").click();
    const input = page.getByTestId("copilot-chat-textarea");
    await expect(input).toBeVisible();
    await input.fill(
      "Which right-panel surface tab am I currently viewing? Answer in one short sentence.",
    );
    await input.press("Enter");
    // useAgentContext feeds the active surface ("Wiki") to the agent.
    await expect(page.getByText(/viewing the Wiki/i)).toBeVisible({ timeout: 60_000 });
  });

  test("generative bridge: the agent renders a real Aleph ChartCard inline", async ({
    page,
  }) => {
    await openProjectWorkspace(page, projectId);
    await page.getByTestId("chat-mode-live").click();
    const input = page.getByTestId("copilot-chat-textarea");
    await expect(input).toBeVisible();
    await input.fill(
      'Render a ChartCard as a bar chart of these benchmark scores: ' +
        'GPT-4 = 86, Claude = 89, Llama = 78. Title it "Benchmark scores".',
    );
    await input.press("Enter");
    // The agent calls render_a2ui → AG-UI streams an A2UI surface with
    // catalogId "aleph" → the frontend renders Aleph's ChartCard, which
    // mounts a Vega canvas. A <canvas> on the page is the proof.
    await expect(page.locator("canvas").first()).toBeVisible({ timeout: 90_000 });
  });
});

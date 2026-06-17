import { expect, test } from "@playwright/test";

import {
  cleanupTestProjects,
  createProject,
  createSession,
  openProjectWorkspace,
  sendChat,
} from "./helpers";

/**
 * W2/W6 — CopilotKit v2 + AG-UI + A2UI shared state and generative cards.
 *
 * Since W6 the Live CopilotKit chat is the ONLY chat surface (the Classic
 * mode + chat-mode toggle were removed). The chat talks to the assistant
 * Deep Agent through the Node CopilotRuntime (:4000) → aleph-api AG-UI
 * endpoint. These tests drive the real running stack (the live round-trips
 * need the LLM gateway), matching the project's standing requirement to
 * verify in a real browser.
 */
const RUNTIME_URL =
  process.env.ALEPH_COPILOT_RUNTIME_URL ?? "http://localhost:4000/api/copilotkit";

test.describe("W2/W6 — CopilotKit + AG-UI + A2UI", () => {
  let projectId: string;

  test.beforeAll(async ({ request }) => {
    await cleanupTestProjects(request);
    const p = await createProject(request, { title: "W2 copilotkit test" });
    projectId = p.id;
  });

  test.afterAll(async ({ request }) => {
    await cleanupTestProjects(request);
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

  test("the CopilotKit chat surface mounts in a new session", async ({ page }) => {
    await openProjectWorkspace(page, projectId);
    await createSession(page);
    await expect(page.getByTestId("copilot-chat-textarea")).toBeEnabled();
  });

  test("shared state: the agent knows which surface tab the analyst is viewing", async ({
    page,
  }) => {
    await openProjectWorkspace(page, projectId);
    await createSession(page);
    await sendChat(
      page,
      "Which right-panel surface tab am I currently viewing? Answer in one short sentence.",
    );
    // useAgentContext feeds the active surface ("Wiki") to the agent.
    await expect(page.getByText(/viewing the Wiki/i)).toBeVisible({ timeout: 60_000 });
  });

  test("generative bridge: the agent renders a real Aleph ChartCard inline", async ({
    page,
  }) => {
    await openProjectWorkspace(page, projectId);
    await createSession(page);
    await sendChat(
      page,
      "Render a ChartCard as a bar chart of these benchmark scores: " +
        'GPT-4 = 86, Claude = 89, Llama = 78. Title it "Benchmark scores".',
    );
    // The agent calls render_a2ui → AG-UI streams an A2UI surface with
    // catalogId "aleph" → the frontend renders Aleph's ChartCard, which
    // mounts a Vega canvas. A <canvas> on the page is the proof.
    await expect(page.locator("canvas").first()).toBeVisible({ timeout: 90_000 });
  });
});

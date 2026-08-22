import { expect, test } from "@playwright/test";

import { composer, createProject, deleteProject, newSession, openWorkspace } from "./helpers";

/**
 * claim: chat-streams-response — send a message, get a streamed reply.
 *
 * Harvested from `audit/checks/e2e/chat-streams-response.spec.ts`. This is the
 * one spec in the suite that needs a reachable gateway: it drives the browser →
 * copilot-runtime bridge → AG-UI endpoint → agent → gateway path end to end,
 * which is the path no unit test can stand in for and the one that broke
 * silently twice (an endpoint exempt from auth, and a stream that just stopped
 * on error rather than emitting RUN_ERROR).
 *
 * The assertion is on non-empty streamed TEXT, not on an element appearing. An
 * assistant bubble with nothing in it is exactly what a failed run renders.
 */
test("sending a chat message streams back a non-empty assistant reply", async ({ page, request }) => {
  test.setTimeout(180_000);
  const p = await createProject(
    request,
    "chat project",
    "A short research project about photosynthesis.",
  );
  try {
    await openWorkspace(page, p.id);
    await newSession(page);

    await composer(page).fill("In one sentence, what is this project about?");
    await composer(page).press("Enter");

    await expect(page.getByTestId("copilot-user-message").first()).toBeVisible({ timeout: 30_000 });

    const assistant = page.getByTestId("copilot-assistant-message").last();
    await expect(assistant).toBeVisible({ timeout: 120_000 });
    await expect
      .poll(async () => (await assistant.innerText()).trim().length, { timeout: 120_000 })
      .toBeGreaterThan(0);
  } finally {
    await deleteProject(request, p.id);
  }
});

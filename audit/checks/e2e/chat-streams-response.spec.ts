import { expect, test } from "@playwright/test";

import { createProject, deleteProject, newSession, openWorkspace } from "./helpers";

// claim: chat-streams-response — send a message, get a streamed assistant reply.
test("send a chat message and receive a streamed assistant response", async ({ page, request }) => {
  test.setTimeout(120_000);
  const p = await createProject(request, "chat project", "A short research project about photosynthesis.");
  try {
    await openWorkspace(page, p.id);
    await newSession(page);

    const composer = page.getByTestId("copilot-chat-textarea");
    await composer.fill("In one sentence, what is this project about?");
    await composer.press("Enter");

    // The user's message must appear.
    await expect(page.getByTestId("copilot-user-message").first()).toBeVisible({ timeout: 20_000 });

    // An assistant message with non-empty text must stream in.
    const assistant = page.getByTestId("copilot-assistant-message").first();
    await expect(assistant).toBeVisible({ timeout: 90_000 });
    await expect
      .poll(async () => (await assistant.innerText()).trim().length, { timeout: 90_000 })
      .toBeGreaterThan(0);
  } finally {
    await deleteProject(request, p.id);
  }
});

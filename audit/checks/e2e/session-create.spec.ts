import { expect, test } from "@playwright/test";

import { createProject, deleteProject, newSession, openWorkspace } from "./helpers";

// claim: session-create — '+ New' creates a session and enables the composer.
test("'+ New' creates a session and enables the chat composer", async ({ page, request }) => {
  const p = await createProject(request, "session project");
  try {
    await openWorkspace(page, p.id);
    await newSession(page);
    const composer = page.getByTestId("copilot-chat-textarea");
    await expect(composer).toBeVisible();
    await expect(composer).toBeEnabled();
  } finally {
    await deleteProject(request, p.id);
  }
});

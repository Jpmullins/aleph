import { expect, test } from "@playwright/test";

import { composer, createProject, deleteProject, newSession, openWorkspace } from "./helpers";

/**
 * claim: session-create — creating a session enables the chat composer.
 *
 * Harvested from `audit/checks/e2e/session-create.spec.ts`. The button moved:
 * "+ New" lived in the deleted left panel, and session switching now sits in
 * the assistant dock next to the conversation, because a session is a property
 * of the conversation rather than of the project.
 *
 * "Enabled" is asserted separately from "visible" on purpose. The composer
 * renders before a thread exists, and typing into a disabled textarea loses the
 * keystrokes without an error anywhere.
 */
test("creating a session from the dock enables the chat composer", async ({ page, request }) => {
  const p = await createProject(request, "session project");
  try {
    await openWorkspace(page, p.id);
    await newSession(page);
    await expect(composer(page)).toBeVisible();
    await expect(composer(page)).toBeEnabled();
  } finally {
    await deleteProject(request, p.id);
  }
});

test("the new session appears in the dock's session picker", async ({ page, request }) => {
  const p = await createProject(request, "session listed project");
  try {
    await openWorkspace(page, p.id);
    await expect(page.getByTestId("dock-session-select")).toHaveValue("");
    await newSession(page);
    await expect(page.getByTestId("dock-session-select")).not.toHaveValue("");
  } finally {
    await deleteProject(request, p.id);
  }
});

import { test, expect } from "@playwright/test";
import { createProject, deleteProject, openWorkspace, newSession, composer } from "./helpers";

test("probe: what does the browser see", async ({ page, request }) => {
  const p = await createProject(request, "probe project", "A short project.");
  const consoleMsgs: string[] = [];
  const responses: string[] = [];
  page.on("console", (m) => consoleMsgs.push(`[${m.type()}] ${m.text()}`.slice(0, 300)));
  page.on("pageerror", (e) => consoleMsgs.push(`[pageerror] ${e.message}`.slice(0, 300)));
  page.on("response", async (r) => {
    if (r.url().includes("copilotkit")) {
      responses.push(`${r.status()} ${r.request().method()} ${r.url()}`);
    }
  });
  try {
    await openWorkspace(page, p.id);
    await newSession(page);
    await composer(page).fill("Say the word OK and nothing else.");
    await composer(page).press("Enter");
    await page.waitForTimeout(45000);
    console.log("=== RESPONSES ===");
    for (const r of responses) console.log(r);
    console.log("=== CONSOLE ===");
    for (const c of consoleMsgs) console.log(c);
    const dock = await page.getByTestId("assistant-dock").innerText();
    console.log("=== DOCK TEXT ===");
    console.log(dock.slice(0, 1500));
  } finally {
    await deleteProject(request, p.id);
  }
});

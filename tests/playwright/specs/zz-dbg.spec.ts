import { test } from "@playwright/test";
import { createProject, deleteProject, openWorkspace } from "./helpers";

test("debug settings pane", async ({ page, request }) => {
  page.on("console", (m) => console.log("CONSOLE", m.type(), m.text().slice(0, 400)));
  page.on("pageerror", (e) => console.log("PAGEERROR", e.message.slice(0, 400)));
  const p = await createProject(request, "debug settings");
  try {
    await openWorkspace(page, p.id);
    await page.getByTestId("rail-settings").click();
    await page.waitForTimeout(6000);
    const html = await page.locator('[data-pane-kind="Settings"]').innerHTML();
    console.log("PANE HTML:", html.slice(0, 2000));
  } finally {
    await deleteProject(request, p.id);
  }
});

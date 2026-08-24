import { test } from "@playwright/test";
import { openWorkspace } from "./helpers";

test("inspector renders a real run timeline", async ({ page }) => {
  test.setTimeout(120_000);
  const errors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  await openWorkspace(page, "01a02488-d28e-7791-846f-854b7df6593e");
  await page.getByRole("button", { name: "Inspector" }).click();
  await page.waitForTimeout(8000);
  const body = await page.locator("main").innerText();
  console.log("=== MAIN TEXT ===\n" + body.slice(0, 2500));
  console.log("=== CONSOLE ERRORS (" + errors.length + ") ===\n" + errors.slice(0, 12).join("\n"));
});

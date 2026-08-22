import { expect, test } from "@playwright/test";

import { createProject, deleteProject, openWorkspace } from "./helpers";

/**
 * claim: the theme can be changed from inside a project. WS-B1 #5.
 *
 * FAILED TODAY: `ThemeToggle` existed, worked, and was rendered in exactly one
 * place — the project LIST. Once you opened a project the only way to change
 * the theme was to leave the project, which puts the control behind the thing
 * you want to look at while using it.
 *
 * The assertion is on `document.documentElement.dataset.theme`, as the plan
 * states it, because that attribute is what every token in `tokens.css` keys
 * off. Asserting a class name or a pixel would pass on a control that flipped
 * something nothing reads.
 */
test("activating the theme control inside the workspace flips data-theme", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "theme inside project");
  try {
    await openWorkspace(page, p.id);

    // Inside the workspace chrome specifically. Scoping to the context bar is
    // the half that fails if the control is only on the project list: an
    // unscoped locator would find a toggle anywhere on the page, and the point
    // of this criterion is WHERE it is.
    const bar = page.getByTestId("context-bar");
    await expect(bar.getByRole("group", { name: "Display theme" })).toBeVisible();

    const before = await page.evaluate(() => document.documentElement.dataset.theme);
    const target = before === "dark" ? "Light theme" : "Dark theme";
    await bar.getByRole("button", { name: target }).click();

    await expect
      .poll(() => page.evaluate(() => document.documentElement.dataset.theme))
      .toBe(before === "dark" ? "light" : "dark");

    // And back, so the test does not pass on a control that can only ever set
    // one value — which is what a one-way "enable dark mode" button would be.
    const back = before === "dark" ? "Dark theme" : "Light theme";
    await bar.getByRole("button", { name: back }).click();
    await expect
      .poll(() => page.evaluate(() => document.documentElement.dataset.theme))
      .toBe(before === "dark" ? "dark" : "light");
  } finally {
    await deleteProject(request, p.id);
  }
});

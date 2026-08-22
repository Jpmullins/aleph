import { expect, test } from "@playwright/test";

import { cleanupE2EProjects, createProject, deleteProject, openWorkspace } from "./helpers";

/**
 * claim: Escape closes every modal and focus is trapped inside it. WS-B1 #4.
 *
 * FAILED TODAY, and not subtly: `git grep -c '"Escape"' apps/web/src` returned
 * ZERO FILES across the whole app. Two of the four things that called
 * themselves dialogs carried `role="dialog" aria-modal="true"` — a promise to a
 * screen reader that everything behind them is inert — and Tab walked straight
 * out into the page behind while no key closed anything.
 *
 * The plan states this criterion as a Playwright test for a reason a unit test
 * cannot cover: a focus trap is a claim about where the BROWSER puts focus, and
 * jsdom's `document.activeElement` does not model tab order at all. It reports
 * `<body>` for a Tab it never simulated, so a jsdom "focus trap" test passes
 * against a component with no trap in it.
 *
 * Three separate properties, three separate tests. A single test asserting all
 * of them is one red line for three different defects.
 */

test.beforeAll(async ({ request }) => {
  await cleanupE2EProjects(request);
});

test("Tab cycles inside the dialog and never reaches the page behind", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "focus trap project");
  try {
    await openWorkspace(page, p.id);
    await page.getByTestId("context-upload-source").click();
    const dialog = page.getByTestId("source-upload-modal");
    await expect(dialog).toBeVisible();

    // Five presses, per the plan. The dialog has fewer than five focusables, so
    // this necessarily wraps — which is the property under test. A count equal
    // to the number of controls would pass against a component with no trap.
    for (let i = 0; i < 5; i += 1) {
      await page.keyboard.press("Tab");
      const inside = await page.evaluate(() => {
        const active = document.activeElement;
        const panel = document.querySelector('[data-testid="source-upload-modal"]');
        return Boolean(active && panel && (panel === active || panel.contains(active)));
      });
      expect(inside, `focus left the dialog after ${i + 1} Tab press(es)`).toBe(true);
    }

    // Backwards too. Shift+Tab off the first control must wrap to the last, not
    // step out into the workspace.
    for (let i = 0; i < 5; i += 1) {
      await page.keyboard.press("Shift+Tab");
      const inside = await page.evaluate(() => {
        const active = document.activeElement;
        const panel = document.querySelector('[data-testid="source-upload-modal"]');
        return Boolean(active && panel && (panel === active || panel.contains(active)));
      });
      expect(inside, `focus left the dialog after ${i + 1} Shift+Tab press(es)`).toBe(true);
    }
  } finally {
    await deleteProject(request, p.id);
  }
});

test("Escape closes the dialog and returns focus to the control that opened it", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "escape closes project");
  try {
    await openWorkspace(page, p.id);
    const trigger = page.getByTestId("context-upload-source");
    await trigger.click();
    await expect(page.getByTestId("source-upload-modal")).toBeVisible();

    await page.keyboard.press("Escape");
    await expect(page.getByTestId("source-upload-modal")).toHaveCount(0);

    // Focus RETURNS. Without this half, closing a dialog drops focus on
    // `<body>` and the next Tab restarts from the top of the document — which
    // is what every one of these dialogs did before WS-B1.
    await expect(trigger).toBeFocused();
  } finally {
    await deleteProject(request, p.id);
  }
});

test("Escape typed inside a text field still closes the dialog", async ({ page, request }) => {
  // The realistic case, and the one a naive `window.addEventListener` gets
  // wrong: the person is mid-typing when they change their mind. If the handler
  // is bound above the field, or if the field stops propagation, Escape does
  // nothing and the only way out is the mouse.
  const p = await createProject(request, "escape from field project");
  try {
    await openWorkspace(page, p.id);
    await page.getByTestId("context-upload-source").click();
    const dialog = page.getByTestId("source-upload-modal");
    await expect(dialog).toBeVisible();

    const title = dialog.locator('input[type="text"], input:not([type])').first();
    await title.click();
    await title.fill("half a title");
    await page.keyboard.press("Escape");

    await expect(page.getByTestId("source-upload-modal")).toHaveCount(0);
  } finally {
    await deleteProject(request, p.id);
  }
});

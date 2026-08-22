import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

/**
 * WS-E3 — the app's typography, and the compiled document's theme, in a browser.
 *
 * Three claims that only a real browser can settle:
 *
 *   1. the three faces render from files this repository ships, with every
 *      non-local host unreachable — which is what a docker compose install on a
 *      private network is;
 *   2. the app shell asks no host outside the compose stack for anything;
 *   3. the server-compiled wiki document lands on the same ground the app does.
 *
 * (1) is the one that needed a browser. `grep`ping index.html for a CDN link
 * proves the link is gone, not that anything replaced it — a `@font-face`
 * pointing at a path that 404s produces exactly the same silent fallback the
 * CDN did, and neither the build nor any unit test would say a word.
 */

const REPO_ROOT = fileURLToPath(new URL("../../..", import.meta.url));

/** Hosts the compose stack actually runs on. Anything else is "the internet". */
const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

function isLocal(url: string): boolean {
  if (!/^https?:/i.test(url)) return true; // data:, blob:, about: — nothing leaves the machine
  try {
    return LOCAL_HOSTS.has(new URL(url).hostname);
  } catch {
    return false;
  }
}

/** The three families tokens.css names, and the app's real fallback for each. */
const FACES = ["Public Sans", "JetBrains Mono", "Newsreader"] as const;

test.describe("the app renders in its own type with no route off the machine", () => {
  test("all three faces load from this repository while every remote host is dead", async ({
    page,
  }) => {
    const blocked: string[] = [];
    const fontRequests: string[] = [];

    // Harsher than the plan's criterion, which aborts the two Google hosts.
    // Aborting EVERYTHING non-local is the actual deployment condition, and it
    // is the only version of this test that cannot be satisfied by swapping one
    // CDN for another.
    await page.route("**/*", async (route) => {
      const url = route.request().url();
      if (!isLocal(url)) {
        blocked.push(url);
        await route.abort();
        return;
      }
      if (url.includes(".woff2")) fontRequests.push(new URL(url).pathname);
      await route.continue();
    });

    await page.goto("/");
    await page.waitForLoadState("domcontentloaded");

    // Ask for each family explicitly. `document.fonts.check` alone is not an
    // assertion: it answers "is this loaded", and a face nothing on screen has
    // used yet is legitimately not loaded, so a green check could mean either
    // "the font works" or "the test asked at the right moment".
    const loaded = await page.evaluate(async (families: readonly string[]) => {
      const out: Record<string, { faces: number; check: boolean }> = {};
      for (const family of families) {
        const faces = await document.fonts.load(`400 1em "${family}"`, "Aa 0123");
        out[family] = { faces: faces.length, check: document.fonts.check(`1em "${family}"`) };
      }
      return out;
    }, FACES);

    for (const family of FACES) {
      expect(loaded[family].faces, `no @font-face matched "${family}"`).toBeGreaterThan(0);
      expect(loaded[family].check, `"${family}" declared but did not load`).toBe(true);
    }

    // A control: the same call for a family nothing declares must come back
    // empty, or the two assertions above are measuring the browser's willingness
    // to say yes rather than the presence of a font.
    const nonsense = await page.evaluate(async () => {
      const faces = await document.fonts.load('400 1em "Aleph No Such Face"', "Aa");
      return faces.length;
    });
    expect(nonsense, "document.fonts.load resolves anything — the checks above are vacuous").toBe(
      0,
    );

    // The body must actually be WEARING the UI face, not merely have it
    // available: three declarations of the type stack disagreed once before and
    // Inter won on cascade order, so the system was designed and never applied.
    const bodyFamily = await page.evaluate(() => getComputedStyle(document.body).fontFamily);
    expect(bodyFamily).toContain("Public Sans");

    expect(blocked, `the app tried to reach off the machine: ${blocked.join(", ")}`).toEqual([]);
    // The faces came from THIS origin. Without this the test would also pass on
    // a browser that happened to have Public Sans installed locally.
    expect(fontRequests.length, "no woff2 was fetched at all").toBeGreaterThan(0);
    for (const path of fontRequests) expect(path).toMatch(/^\/fonts\/[a-z-]+\.woff2$/);
  });

  test("the app shell requests nothing from a host outside the compose stack", async ({ page }) => {
    const external: string[] = [];
    page.on("request", (request) => {
      if (!isLocal(request.url())) external.push(`${request.resourceType()} ${request.url()}`);
    });

    await page.goto("/");
    await page.waitForLoadState("networkidle");

    expect(external, `boot reached these external hosts:\n  ${external.join("\n  ")}`).toEqual([]);
  });
});

/**
 * The server-compiled wiki page, rendered exactly the way the reader renders it:
 * a `sandbox=""` iframe over a response carrying `Content-Security-Policy:
 * sandbox`, which is what `apps/api/src/aleph_api/routes/wiki.py` sends and what
 * `apps/web/src/a2ui/components/HtmlDocCard.tsx` mounts.
 *
 * The document is compiled by the REAL compiler, in a subprocess, rather than
 * pasted in as a fixture. A fixture here would assert the colour this file
 * chose, in a file the compiler never reads.
 *
 * It is served through `page.route` rather than fetched from the API because
 * the API container in this environment runs a baked image and does not have
 * the current compiler — measuring through it would measure the last deploy.
 */
const DOC_PATH = "/__ws_e3_compiled_doc__.html";

function compiledDocument(): string {
  return execFileSync(
    "uv",
    [
      "run",
      "python",
      "-c",
      "import sys;from aleph_wiki.html_compiler import compile_page_html;"
      + "sys.stdout.write(compile_page_html(title='Themed document',"
      + "body_md='A paragraph of body text.',"
      + "claims=[{'text':'A supported claim.','confidence':'well_supported'}],"
      + "infobox={'Sources':75}))",
    ],
    { cwd: REPO_ROOT, encoding: "utf8", maxBuffer: 8 * 1024 * 1024 },
  );
}

/** `rgb(12, 14, 16)` → `#0c0e10`, so a hex token and a computed colour compare. */
function toHex(colour: string): string {
  const m = /rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)/.exec(colour.trim());
  if (m) return `#${[m[1], m[2], m[3]].map((n) => Number(n).toString(16).padStart(2, "0")).join("")}`;
  return colour.trim().toLowerCase();
}

for (const scheme of ["light", "dark"] as const) {
  test(`the compiled document lands on the app's ${scheme} ground`, async ({ browser }) => {
    const html = compiledDocument();
    // Proves the subprocess ran the compiler rather than printing a traceback
    // to stdout, which would make every assertion below compare two blanks.
    expect(html).toContain("prefers-color-scheme");

    const context = await browser.newContext({ colorScheme: scheme });
    const page = await context.newPage();
    try {
      await page.route(`**${DOC_PATH}`, (route) =>
        route.fulfill({
          status: 200,
          contentType: "text/html; charset=utf-8",
          headers: { "Content-Security-Policy": "sandbox" },
          body: html,
        }),
      );

      // The app's own ground, read from the running app in this same scheme.
      // Not a constant in this file: the palette lives in tokens.css and a test
      // that restates it would pass forever after the palette changed.
      await page.goto("/");
      const appGround = toHex(
        await page.evaluate(() =>
          getComputedStyle(document.documentElement).getPropertyValue("--surface-bg"),
        ),
      );
      const appTheme = await page.evaluate(() =>
        document.documentElement.getAttribute("data-theme"),
      );
      expect(appTheme, "the app did not follow the emulated colour scheme").toBe(scheme);

      await page.setContent(
        `<body style="margin:0"><iframe id="doc" sandbox="" src="${DOC_PATH}"
           style="width:800px;height:400px;border:0"></iframe></body>`,
      );
      const frame = page.frameLocator("#doc");
      await frame.locator("h1").waitFor();
      const docGround = toHex(
        await frame
          .locator("body")
          .evaluate((body) => getComputedStyle(body).backgroundColor),
      );

      expect(
        docGround,
        `the compiled document painted ${docGround} while the app is on ${appGround}`,
      ).toBe(appGround);
    } finally {
      await context.close();
    }
  });
}

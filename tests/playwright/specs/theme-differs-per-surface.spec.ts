import { expect, test } from "@playwright/test";

import { API_URL, AUTH, createProject, deleteProject, openWorkspace } from "./helpers";

/**
 * claim: no surface renders identically in light and dark. WS-G criterion 6.
 *
 * A hardcoded `text-slate-500` is not a cosmetic problem. It does not respond
 * to the theme at all, so it renders the same on both grounds — which is how
 * an app ends up looking right in one theme and wrong in the other. The token
 * sweep (`scripts/check-web-drift.sh`) counts the ones written as Tailwind
 * classes, and the unit tests check the palette's contrast, but neither can see
 * a colour that arrives some other way: an inline style, a third-party widget's
 * default, an SVG `fill`, a canvas paint. This renders the real thing in both
 * themes and compares the pixels.
 *
 * A surface that does NOT differ is either achromatic by design or carrying a
 * hardcoded colour, and the two are distinguishable only by looking. So a
 * surface may be exempted, by name, in ACHROMATIC below — with its reason — and
 * an exempt surface is still asserted to be exempt for the stated reason
 * (identical), so an exemption that stops being true fails too.
 *
 * Why pixels rather than computed styles: a computed-style sweep reads the
 * elements it thought to ask about. Canvas is the case that matters most here —
 * ChartCard paints axis labels onto a canvas, which has no cascade and no
 * elements to query — and it is exactly where the last hardcoded colour was
 * found.
 */

/** Surfaces allowed to render identically, with the reason each is allowed to.
 *  An entry is a claim that the surface is genuinely achromatic. It is checked:
 *  a surface named here that DOES differ fails, because the exemption is then
 *  stale and hiding a real comparison. */
const ACHROMATIC = new Map<string, string>();

/** How much of the frame must change. A single antialiased glyph edge shifting
 *  is not a theme responding; a ground and its ink both inverting moves most of
 *  the frame. 2% is far above render noise and far below a real theme change,
 *  which measures 40-90% on every surface in this app. */
const MIN_CHANGED_FRACTION = 0.02;

/** Ignore pixels that differ only microscopically — subpixel text rendering is
 *  not deterministic across two paints of the same tree. */
const CHANNEL_NOISE = 8;

/** Fraction of pixels that differ between two PNG screenshots.
 *
 *  Decoded in the browser rather than in Node. Node has no image decoder in
 *  the standard library and the suite has no `pngjs`/`sharp`; adding one to
 *  compare two buffers would be a dependency for arithmetic. The page already
 *  has a decoder — it is the thing that painted these pixels — so the buffers
 *  go back in as data URLs and the comparison runs there.
 */
async function changedFraction(
  page: import("@playwright/test").Page,
  a: Buffer,
  b: Buffer,
): Promise<number> {
  return page.evaluate(
    async ([leftB64, rightB64, noise]) => {
      const decode = async (b64: string) => {
        const res = await fetch(`data:image/png;base64,${b64}`);
        return createImageBitmap(await res.blob());
      };
      const [left, right] = await Promise.all([decode(leftB64 as string), decode(rightB64 as string)]);
      if (left.width !== right.width || left.height !== right.height) {
        throw new Error(
          `screenshots are different sizes (${left.width}x${left.height} vs ` +
            `${right.width}x${right.height}); the comparison would be meaningless`,
        );
      }
      const pixels = (bitmap: ImageBitmap) => {
        const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
        const ctx = canvas.getContext("2d");
        if (!ctx) throw new Error("no 2d context — cannot compare screenshots");
        ctx.drawImage(bitmap, 0, 0);
        return ctx.getImageData(0, 0, bitmap.width, bitmap.height).data;
      };
      const l = pixels(left);
      const r = pixels(right);
      let changed = 0;
      for (let i = 0; i < l.length; i += 4) {
        if (
          Math.abs(l[i] - r[i]) > (noise as number) ||
          Math.abs(l[i + 1] - r[i + 1]) > (noise as number) ||
          Math.abs(l[i + 2] - r[i + 2]) > (noise as number)
        ) {
          changed += 1;
        }
      }
      return changed / (left.width * left.height);
    },
    [a.toString("base64"), b.toString("base64"), CHANNEL_NOISE] as const,
  );
}

async function setTheme(page: import("@playwright/test").Page, mode: "light" | "dark") {
  await page.evaluate((m) => {
    document.documentElement.setAttribute("data-theme", m);
  }, mode);
  // The chart surfaces re-resolve their tokens on a `data-theme` mutation via a
  // MutationObserver (lib/theme-tokens.ts) and re-embed. Wait for that to land
  // rather than racing it, or a canvas is captured mid-repaint and the diff
  // measures timing instead of theme.
  await page.waitForTimeout(400);
}

test("every surface the server declares renders differently in light and dark", async ({
  page,
  request,
}) => {
  const p = await createProject(request, "theme diff project");
  try {
    const resp = await request.get(`${API_URL}/v1/projects/${p.id}/panes`, { headers: AUTH });
    expect(resp.ok()).toBe(true);
    const { panes } = (await resp.json()) as {
      panes: { id: string; title: string; launchable: boolean }[];
    };
    const launchable = panes.filter((k) => k.launchable);
    // A zero-length list would make every assertion below vacuous and this test
    // would pass on an app that serves no surfaces at all.
    expect(launchable.length).toBeGreaterThan(0);

    await openWorkspace(page, p.id);

    const identical: string[] = [];
    const measured: string[] = [];

    for (const pane of launchable) {
      // By ID. The rail's testid used to be the lower-cased TITLE, which is
      // the same string only for a single-word title.
      await page.getByTestId(`rail-${pane.id}`).click();
      // By kind, not by position. `data-testid="block"` is on every block and
      // `.last()` quietly addresses a different pane once MAX_PANES starts
      // closing the oldest one.
      const block = page.locator(`[data-pane-kind="${pane.id}"]`).last();
      await expect(block).toBeVisible();

      await setTheme(page, "light");
      const light = await block.screenshot();
      await setTheme(page, "dark");
      const dark = await block.screenshot();

      const fraction = await changedFraction(page, light, dark);
      measured.push(`${pane.title} ${(fraction * 100).toFixed(1)}%`);
      const exempt = ACHROMATIC.has(pane.title);
      if (fraction < MIN_CHANGED_FRACTION) {
        if (!exempt) identical.push(`${pane.title} (${(fraction * 100).toFixed(2)}% changed)`);
      } else if (exempt) {
        throw new Error(
          `${pane.title} is listed in ACHROMATIC as "${ACHROMATIC.get(pane.title)}", ` +
            `but it changed ${(fraction * 100).toFixed(1)}% between themes. ` +
            `The exemption is stale — remove it.`,
        );
      }
    }

    console.log(`theme delta per surface: ${measured.join(", ")}`);
    expect(
      identical,
      `these surfaces render the same in both themes, so something in them is ` +
        `not reading a token — an inline style, an SVG fill, or a canvas paint. ` +
        `Measured: ${measured.join(", ")}`,
    ).toEqual([]);
  } finally {
    await deleteProject(request, p.id);
  }
});

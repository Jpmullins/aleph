/**
 * The palette, checked as data.
 *
 * WS-G moved 96 hardcoded Tailwind palette classes onto the semantic tokens in
 * `tokens.css`. That is only an improvement if the tokens are actually legible
 * on both grounds — otherwise the app has traded 96 visible wrongs for one
 * invisible one, and the drift sweep would report zero either way.
 *
 * So this reads the shipped stylesheet and asserts three things a reviewer
 * cannot see by looking:
 *
 *   1. Every themed token is defined in BOTH theme blocks. The file's own
 *      header records this bug: the accent and the shadows were defined only
 *      inside `[data-theme="dark"]`, so a viewer in the default "system" state
 *      — who never touched the toggle, which is most people — got a dark page
 *      wearing the light accent.
 *   2. The two dark blocks are byte-identical in what they declare. They are
 *      hand-maintained duplicates; nothing else would notice them drifting.
 *   3. Contrast. ChartCard's hardcoded slate-600 axis labels over the dark
 *      raised surface shipped at 2.4:1 for months, because no screenshot of a
 *      single theme can find it. Every foreground/background pair the app
 *      actually uses is checked here, in both themes, against WCAG AA.
 *
 * This asserts on production values, not on a fixture: the stylesheet is
 * imported raw from the same path the app imports for real.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

/**
 * Read from disk rather than `import "./tokens.css?raw"`.
 *
 * vitest stubs a CSS import to the empty string unless `test.css` is enabled,
 * and an empty stylesheet parses to zero tokens — every assertion below would
 * iterate an empty list and the suite would stay green while checking nothing.
 * That is the failure shape this repo keeps finding, so the read is explicit
 * and a missing file throws by name. `import.meta.url` is an http URL under
 * jsdom, so it cannot be the anchor; vitest sets cwd to the config root.
 */
const REL = "src/styles/tokens.css";
const CANDIDATES = [resolve(process.cwd(), REL), resolve(process.cwd(), "apps/web", REL)];
const tokensPath = CANDIDATES.find(existsSync);
if (!tokensPath) throw new Error(`tokens.css not found; looked in ${CANDIDATES.join(", ")}`);
const tokensCss = readFileSync(tokensPath, "utf8");

type Tokens = Record<string, string>;

/** The block bounded by `selector {` and its first closing brace. */
function block(css: string, opener: string): Tokens {
  const start = css.indexOf(opener);
  if (start === -1) throw new Error(`tokens.css has no \`${opener}\` block`);
  const body = css.slice(start + opener.length, css.indexOf("}", start));
  const out: Tokens = {};
  for (const [, name, value] of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    out[name] = value.trim();
  }
  return out;
}

// Comments in tokens.css quote example declarations — the note explaining why
// `--badge-warning-*` exists spells out the fallback that used to stand in for
// it — and a declaration parser that read them would report tokens the file
// does not actually define.
const css = tokensCss.replace(/\/\*[\s\S]*?\*\//g, "");

const light = block(css, ":root {");
const dark = block(css, '[data-theme="dark"] {');
const systemDark = block(css, ':root:not([data-theme="light"]) {');

/**
 * Tokens that are the same on both grounds by design and are therefore
 * declared once. A radius and a type stack have no light and dark version.
 */
const THEME_INVARIANT = new Set(["--radius", "--font-ui", "--font-mono", "--font-prose"]);

function srgbToLinear(channel: number): number {
  const c = channel / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) throw new Error(`not a 6-digit hex colour: ${hex}`);
  const n = parseInt(m[1], 16);
  return (
    0.2126 * srgbToLinear((n >> 16) & 0xff) +
    0.7152 * srgbToLinear((n >> 8) & 0xff) +
    0.0722 * srgbToLinear(n & 0xff)
  );
}

function contrast(fg: string, bg: string): number {
  const a = luminance(fg);
  const b = luminance(bg);
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

const THEMES: [string, Tokens][] = [
  ["light", light],
  ["dark", dark],
];

/** Grounds any body text can land on. `--surface-overlay` is deliberately out:
 *  it is a translucent scrim, so what shows through it is not a fixed colour. */
const GROUNDS = ["--surface-bg", "--surface-raised", "--surface-sunken", "--surface-elevated"];

describe("tokens.css defines every themed token in every theme block", () => {
  it("declares the same token names in light and dark", () => {
    const themed = Object.keys(light).filter((k) => !THEME_INVARIANT.has(k));
    expect(Object.keys(dark).sort()).toEqual(themed.sort());
  });

  it("keeps the two dark blocks in exact agreement", () => {
    // `[data-theme="dark"]` covers the explicit choice; the
    // `prefers-color-scheme` copy covers the default "system" state, which
    // stamps no attribute at all. They are hand-maintained duplicates.
    expect(systemDark).toEqual(dark);
  });

  it("redefines nothing theme-invariant in a theme block", () => {
    for (const name of THEME_INVARIANT) {
      expect(dark[name], `${name} must not be redefined per theme`).toBeUndefined();
    }
  });
});

/** Every ink token, read off the file rather than listed here.
 *
 *  The list used to be literal — `["--text-primary", "--text-secondary",
 *  "--accent"]` — which meant the gate pinned today's palette and could not
 *  see tomorrow's. Adding a `--text-notice` near-black to all three blocks,
 *  roughly 1.0:1 on the dark raised ground, left all 64 assertions green. A
 *  contrast gate that a new illegible token walks straight past is a gate for
 *  the tokens somebody remembered, not for the palette.
 *
 *  So the inks are derived: every `--text-*` and every `--state-*` in the
 *  file. A new token is covered the moment it is declared, and the only way to
 *  opt one out is to name it in QUIET or NOT_INK below, in the same change,
 *  where a reviewer sees the exemption instead of the omission. */
const inkTokens = (prefix: string) =>
  Object.keys(light)
    .filter((name) => name.startsWith(prefix))
    .sort();

/** Tokens held to the 3:1 quiet tier rather than 4.5:1 — the deliberately
 *  recessive ones: timestamps, counts, placeholder hints. An entry here is a
 *  decision that the token is non-essential text, and it still may never drop
 *  below the floor where it stops being readable at all. */
const QUIET = new Set(["--text-muted"]);

/** `--text-*` names that are not inks: a ground, or a colour only ever paired
 *  with one specific background checked elsewhere. Each needs its reason. */
const NOT_INK = new Map<string, string>([
  // Paired with `--accent`, not with a page ground; asserted on its own below.
  ["--text-on-accent", "checked against --accent, not against page grounds"],
  // The ink for an INVERTED ground: `bg-ink text-ink-inverse` on the primary
  // button. styles.css maps `--color-ink` to `--text-primary`, so its ground
  // is that token, and it is asserted against it below. Against a page ground
  // it is deliberately illegible — that is what "inverse" means.
  ["--text-inverse", "checked against --text-primary, the ground bg-ink paints"],
]);

describe("every text/ground pair clears WCAG AA in both themes", () => {
  it("has some inks to check — an empty derivation would pass vacuously", () => {
    const inks = [...inkTokens("--text-"), ...inkTokens("--state-")];
    expect(inks.length).toBeGreaterThanOrEqual(4);
  });

  for (const [theme, t] of THEMES) {
    for (const ground of GROUNDS) {
      for (const ink of [...inkTokens("--text-"), ...inkTokens("--state-"), "--accent"]) {
        if (NOT_INK.has(ink)) continue;
        // 4.5:1 is AA for body text at normal weight and size, which is what
        // Aleph's chrome is — 10 to 13px, dense. State colour has to survive
        // being read as text, not just as a wash: `text-good` / `text-bad` are
        // how a failure is spelled now.
        const floor = QUIET.has(ink) ? 3 : 4.5;
        const tier = QUIET.has(ink) ? " (quiet tier)" : "";
        it(`${theme}: ${ink} on ${ground}${tier}`, () => {
          expect(t[ink], `${ink} is not declared in the ${theme} block`).toBeTruthy();
          expect(contrast(t[ink], t[ground])).toBeGreaterThanOrEqual(floor);
        });
      }
    }

    // Every status badge, against its OWN ground rather than the page's. These
    // are the pairs WS-G moved 96 palette classes onto; if one of them is
    // illegible the sweep still reports zero drift. The states are derived
    // from the file for the same reason the inks are.
    const badgeStates = Object.keys(light)
      .map((name) => /^--badge-(.+)-fg$/.exec(name)?.[1])
      .filter((state): state is string => Boolean(state))
      .sort();

    it(`${theme}: the badge states were found, not assumed`, () => {
      expect(badgeStates.length).toBeGreaterThanOrEqual(5);
    });

    for (const state of badgeStates) {
      it(`${theme}: --badge-${state}-fg on --badge-${state}-bg`, () => {
        expect(
          t[`--badge-${state}-bg`],
          `--badge-${state}-fg has no matching -bg`,
        ).toBeTruthy();
        expect(contrast(t[`--badge-${state}-fg`], t[`--badge-${state}-bg`])).toBeGreaterThanOrEqual(
          4.5,
        );
      });
    }

    // The inverted pair, which the page-ground loop skips by construction:
    // `bg-ink text-ink-inverse`. styles.css:26,29 map `--color-ink` to
    // `--text-primary` and `--color-ink-inverse` to `--text-inverse`, so this
    // is the real pair the primary button renders.
    it(`${theme}: --text-inverse on --text-primary (the bg-ink ground)`, () => {
      expect(contrast(t["--text-inverse"], t["--text-primary"])).toBeGreaterThanOrEqual(4.5);
    });

    it(`${theme}: --accent-fg on --accent`, () => {
      expect(contrast(t["--accent-fg"], t["--accent"])).toBeGreaterThanOrEqual(4.5);
    });
  }
});

describe("the axis colour ChartCard now resolves is legible on the dark ground", () => {
  /**
   * The literal defect: ChartCard hardcoded slate-600 for axis labels and
   * slate-900 for axis titles. Both sit close to the light-theme tokens and are
   * catastrophic on the dark one — slate-600 over the dark raised surface
   * measures 2.4:1, well under the 4.5:1 floor asserted below, and it is baked
   * into a canvas bitmap where no CSS override can reach it.
   *
   * The replaced literal is described rather than written: `check-web-drift.sh`
   * counts a raw hex anywhere under apps/web/src, test files included, and a
   * pin of zero should not have an exception carved into it for one comment.
   */
  it("--text-secondary over --surface-raised clears AA in dark", () => {
    expect(contrast(dark["--text-secondary"], dark["--surface-raised"])).toBeGreaterThanOrEqual(4.5);
  });
});

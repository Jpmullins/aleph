import { useEffect, useState } from "react";

/**
 * Reading a design token from JavaScript, and knowing when it changed.
 *
 * A canvas has no cascade. Vega paints axis labels into a `<canvas>` bitmap, so
 * it needs a literal colour string and cannot be handed `var(--text-secondary)`
 * — which is exactly why `ChartCard` shipped a hardcoded slate-600 axis label
 * and a slate-900 axis title. On the bone ground those sit close enough to the
 * real tokens that nobody noticed; on the near-black ground the label lands at
 * a contrast ratio of 2.4:1 against the raised surface and the axis is
 * unreadable. No screenshot of a single theme can find that, which is why it
 * survived. `theme-tokens.test.ts` computes that ratio from the shipped
 * tokens.css rather than restating a number here, so this comment cannot go
 * stale against the palette.
 *
 * `readToken` resolves the live computed value instead, and `useThemeEpoch`
 * says when to read it again. Both are needed: a chart that reads the token
 * once and never re-reads it is correct until the moment somebody uses the
 * theme toggle, at which point it is wrong and silent.
 */

/**
 * The current value of a CSS custom property on the document root.
 *
 * Returns "" when the property is not defined. Deliberately NOT a hardcoded
 * fallback: a literal here is a colour with no theme behind it, which is the
 * defect this module exists to remove — fourteen call sites named the accent
 * token with an orange literal after the comma, an accent from a design Aleph
 * no longer has, and that orange rendered whenever the token was absent, in
 * whichever theme that happened to be. An empty return means
 * `tokens.css` did not load, and the caller should omit the property rather
 * than paint a guess.
 */
export function readToken(name: string): string {
  if (typeof document === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * A counter that increments whenever the effective theme changes.
 *
 * Two sources, because the theme has three states and only two of them stamp
 * an attribute: an explicit choice sets `data-theme` on `<html>` (ThemeToggle),
 * and the default "system" state stamps nothing at all and is carried by
 * `prefers-color-scheme`. Watching only the attribute means a viewer who never
 * touched the toggle — most of them — never gets a re-read when the OS flips at
 * sunset.
 */
export function useThemeEpoch(): number {
  const [epoch, setEpoch] = useState(0);

  useEffect(() => {
    const bump = () => setEpoch((n) => n + 1);

    const observer = new MutationObserver(bump);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });

    // `matchMedia` is absent in some test environments and in older embedded
    // webviews; the attribute watcher above still works without it.
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    mq?.addEventListener("change", bump);

    return () => {
      observer.disconnect();
      mq?.removeEventListener("change", bump);
    };
  }, []);

  return epoch;
}

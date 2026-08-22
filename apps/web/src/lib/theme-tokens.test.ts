/**
 * `useThemeEpoch` has to watch TWO things, and the second is the one that gets
 * dropped.
 *
 * The theme has three states. An explicit choice stamps `data-theme` on
 * `<html>`, and `ChartCard.test.tsx` covers that leg end to end. The default
 * "system" state stamps nothing at all — it is carried entirely by
 * `prefers-color-scheme`, and it is the state most viewers are in, because it
 * is what you get by never touching the toggle. A hook that watches only the
 * attribute is correct for everyone who has pressed the button and silently
 * broken for everyone who has not, which is the harder failure to notice.
 *
 * `matchMedia` is stubbed because jsdom's own MediaQueryList cannot be made to
 * change. The subject is still production code: what is asserted is that the
 * hook subscribes, re-renders on a change, and unsubscribes on unmount.
 */
import { renderHook, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { readToken, useThemeEpoch } from "@/lib/theme-tokens";

interface FakeMql {
  listeners: (() => void)[];
  removed: number;
  addEventListener: (type: string, fn: () => void) => void;
  removeEventListener: (type: string, fn: () => void) => void;
}

function stubMatchMedia(): FakeMql {
  const mql: FakeMql = {
    listeners: [],
    removed: 0,
    addEventListener: (_type, fn) => {
      mql.listeners.push(fn);
    },
    removeEventListener: (_type, fn) => {
      mql.removed += 1;
      mql.listeners = mql.listeners.filter((l) => l !== fn);
    },
  };
  vi.stubGlobal("matchMedia", () => mql);
  return mql;
}

afterEach(() => {
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.style.removeProperty("--accent");
});

describe("readToken", () => {
  it("returns the live value of a custom property on the root", () => {
    // A sentinel rather than a colour: what is asserted is that the value
    // travelled from the custom property to the caller, and a real colour could
    // match by coincidence. It also keeps this file clear of the literals
    // `check-web-drift.sh` counts everywhere under apps/web/src, tests included.
    document.documentElement.style.setProperty("--accent", "SENTINEL-accent");
    expect(readToken("--accent")).toBe("SENTINEL-accent");
  });

  it("returns empty rather than a fallback literal for an undefined token", () => {
    // The whole point of the module: no literal is ever substituted, because a
    // substituted literal is a colour with no theme behind it that appears only
    // when the stylesheet fails.
    expect(readToken("--no-such-token")).toBe("");
  });
});

describe("useThemeEpoch", () => {
  it("bumps when the OS preference changes with no data-theme set", () => {
    const mql = stubMatchMedia();
    const { result } = renderHook(() => useThemeEpoch());
    const before = result.current;

    expect(mql.listeners).toHaveLength(1);
    act(() => {
      for (const fn of mql.listeners) fn();
    });

    expect(result.current).toBeGreaterThan(before);
  });

  it("bumps when data-theme is stamped on the root", async () => {
    stubMatchMedia();
    const { result } = renderHook(() => useThemeEpoch());
    const before = result.current;

    await act(async () => {
      document.documentElement.setAttribute("data-theme", "dark");
      // MutationObserver records are delivered as a microtask.
      await Promise.resolve();
    });

    expect(result.current).toBeGreaterThan(before);
  });

  it("unsubscribes from the media query on unmount", () => {
    const mql = stubMatchMedia();
    const { unmount } = renderHook(() => useThemeEpoch());
    unmount();
    expect(mql.removed).toBe(1);
    expect(mql.listeners).toHaveLength(0);
  });

  it("still works where matchMedia does not exist", async () => {
    // Older embedded webviews and some test environments have no matchMedia.
    // The attribute watcher must survive that rather than throwing at mount.
    vi.stubGlobal("matchMedia", undefined);
    const { result } = renderHook(() => useThemeEpoch());
    const before = result.current;

    await act(async () => {
      document.documentElement.setAttribute("data-theme", "dark");
      await Promise.resolve();
    });

    expect(result.current).toBeGreaterThan(before);
  });
});

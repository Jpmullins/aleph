/**
 * The theme toggle writes to `document.documentElement`, and two consumers read
 * it back: Aleph's own tokens key off `data-theme`, and CopilotKit v2 keys its
 * dark styles off a `.dark` CLASS. Setting one and not the other gives a dark
 * workspace with a white chat panel — a mismatch that looks like a styling bug
 * in the chat rather than a missing line here.
 */
import { fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThemeToggle } from "@/components/ThemeToggle";

function stubMatchMedia(prefersDark: boolean) {
  // jsdom ships no matchMedia. The component calls it optionally, so without a
  // stub the OS-preference branch is never taken and the "first visit follows
  // the OS" test would pass by never running the code it names.
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches: prefersDark && query.includes("dark"),
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  }));
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.classList.remove("dark");
  stubMatchMedia(false);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ThemeToggle", () => {
  it("seeds the first visit from the OS preference", () => {
    stubMatchMedia(true);
    render(<ThemeToggle />);
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });

  it("prefers an explicit stored choice over the OS preference", () => {
    stubMatchMedia(true);
    window.localStorage.setItem("aleph.theme", "light");
    render(<ThemeToggle />);
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
  });

  it("sets BOTH data-theme and the .dark class CopilotKit reads", () => {
    const view = render(<ThemeToggle />);
    fireEvent.click(view.getByTestId("theme-toggle"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("removes the .dark class again on the way back to light", () => {
    const view = render(<ThemeToggle />);
    fireEvent.click(view.getByTestId("theme-toggle"));
    fireEvent.click(view.getByTestId("theme-light"));
    expect(document.documentElement.getAttribute("data-theme")).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("persists the choice, so a reload does not undo it", () => {
    const view = render(<ThemeToggle />);
    fireEvent.click(view.getByTestId("theme-toggle"));
    expect(window.localStorage.getItem("aleph.theme")).toBe("dark");
  });

  it("marks exactly one segment pressed", () => {
    const view = render(<ThemeToggle />);
    fireEvent.click(view.getByTestId("theme-toggle"));
    expect(view.getByTestId("theme-toggle").getAttribute("aria-pressed")).toBe("true");
    expect(view.getByTestId("theme-light").getAttribute("aria-pressed")).toBe("false");
  });

  it("survives a localStorage that refuses to write", () => {
    // Private-browsing quota errors are real and throw from setItem. An
    // unguarded write takes the whole workspace down on a theme click.
    const view = render(<ThemeToggle />);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(() => fireEvent.click(view.getByTestId("theme-toggle"))).not.toThrow();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
  });
});

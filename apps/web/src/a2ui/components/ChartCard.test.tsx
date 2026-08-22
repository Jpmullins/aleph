/**
 * The chart's axis colours come from the live theme, not from a literal.
 *
 * Vega paints into a `<canvas>`, which has no cascade, so it cannot be handed
 * `var(--text-secondary)` — it needs a resolved colour string. ChartCard's
 * answer to that was a slate-600 label colour written straight into the embed
 * config. On the bone ground that is close to the real token; on the near-black
 * ground it is illegible,
 * and because it is baked into a bitmap no CSS override can reach it. The
 * contrast numbers for that live in `styles/tokens.test.ts`, which reads the
 * shipped palette; what is checked here is the wiring.
 *
 * Two things have to hold, and the second is the one that gets forgotten: the
 * colour must come from the token, AND the chart must be redrawn when the theme
 * changes. A chart that resolves the token once at mount is correct until
 * somebody presses the theme toggle, at which point it is wrong with nothing
 * reporting it.
 *
 * The assertions read the config object handed to the real `vega-embed` entry
 * point. `vega-embed` is mocked because it needs a canvas jsdom does not
 * provide; everything on ChartCard's side of that boundary is production code.
 */
import { render, waitFor } from "@testing-library/react";
import { act } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const embed = vi.fn();
vi.mock("vega-embed", () => ({ default: (...args: unknown[]) => embed(...args) }));

import { ChartCard } from "@/a2ui/components/ChartCard";
import { SurfaceProvider } from "@/a2ui/surface-context";

import type { A2UIComponent } from "@/a2ui/catalog";

const SPEC = { mark: "bar", data: { values: [{ a: 1 }] } };

const COMPONENT = {
  id: "chart-1",
  component: "ChartCard",
  props: { title: "Throughput", vega_lite_spec: SPEC, chart_id: "c1" },
} as unknown as A2UIComponent;

/**
 * The two token values the component reads, painted onto the document root the
 * way tokens.css does at runtime.
 *
 * Deliberately not colours. A sentinel that could never be a valid colour
 * proves the value travelled from the token to Vega; a plausible grey could
 * match by coincidence with the hardcoded literal still in place.
 */
function paintTheme(textSecondary: string, textPrimary: string): void {
  const root = document.documentElement;
  root.style.setProperty("--text-secondary", textSecondary);
  root.style.setProperty("--text-primary", textPrimary);
}

function axisOf(call: unknown[]): Record<string, unknown> {
  const opts = call[2] as { config: { axis: Record<string, unknown> } };
  return opts.config.axis;
}

function mount() {
  return render(
    <SurfaceProvider projectId="p1" surface="test">
      <ChartCard component={COMPONENT} onAction={() => undefined} />
    </SurfaceProvider>,
  );
}

beforeEach(() => {
  // `restoreMocks` restores spies, not the call log of a bare `vi.fn()`. Without
  // this, `embed.mock.calls[0]` in the second test is the FIRST test's call and
  // the assertion silently checks the wrong render.
  embed.mockReset();
  embed.mockResolvedValue({});
});

afterEach(() => {
  const root = document.documentElement;
  root.style.removeProperty("--text-secondary");
  root.style.removeProperty("--text-primary");
  root.removeAttribute("data-theme");
});

describe("ChartCard axis colour", () => {
  it("takes the axis colours from the live tokens", async () => {
    paintTheme("SENTINEL-label-dark", "SENTINEL-title-dark");
    mount();

    await waitFor(() => expect(embed).toHaveBeenCalled());
    const axis = axisOf(embed.mock.calls[0]);
    expect(axis.labelColor).toBe("SENTINEL-label-dark");
    expect(axis.titleColor).toBe("SENTINEL-title-dark");
  });

  it("carries no hardcoded colour when the tokens are absent", async () => {
    // The stylesheet failing to load must not fall back to a literal — a
    // fallback is a second palette that only appears when the first one fails,
    // and fourteen call sites shipped exactly that — the accent token named
    // with an orange literal after the comma.
    mount();

    await waitFor(() => expect(embed).toHaveBeenCalled());
    const axis = axisOf(embed.mock.calls[0]);
    expect(axis).not.toHaveProperty("labelColor");
    expect(axis).not.toHaveProperty("titleColor");
    // The rest of the axis config still ships, so an absent token degrades the
    // colour only rather than the whole chart.
    expect(axis.labelFontSize).toBe(10);
  });

  it("re-embeds with the new colours when the theme flips", async () => {
    paintTheme("SENTINEL-label-light", "SENTINEL-title-light");
    document.documentElement.setAttribute("data-theme", "light");
    mount();

    await waitFor(() => expect(embed).toHaveBeenCalledTimes(1));
    expect(axisOf(embed.mock.calls[0]).labelColor).toBe("SENTINEL-label-light");

    // What ThemeToggle does: stamp `data-theme` on <html>. tokens.css swaps the
    // custom properties off that attribute, which is simulated here by
    // repainting the two the component reads.
    await act(async () => {
      paintTheme("SENTINEL-label-dark", "SENTINEL-title-dark");
      document.documentElement.setAttribute("data-theme", "dark");
      // MutationObserver callbacks are delivered as a microtask.
      await Promise.resolve();
    });

    await waitFor(() => expect(embed).toHaveBeenCalledTimes(2));
    expect(axisOf(embed.mock.calls[1]).labelColor).toBe("SENTINEL-label-dark");
    expect(axisOf(embed.mock.calls[1]).titleColor).toBe("SENTINEL-title-dark");
  });
});

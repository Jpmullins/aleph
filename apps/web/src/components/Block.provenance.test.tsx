/**
 * The trust meter, and whether it is telling you anything.
 *
 * Every block on the board rendered `band="declarative" trust="signed"` —
 * hardcoded. Four lit bars and a solid edge on a core surface, and four lit
 * bars and a solid edge on a surface a third-party plugin drew. The two things
 * `Block` shows on EVERY frame, in the design's own words "because in a
 * generative interface they are primary rather than metadata", said the same
 * thing about both, so they said nothing.
 *
 * The derivation landed (`blockProvenance`) and nothing asserted it. A
 * hardcoded pair and a derived pair that happens to return the same value are
 * indistinguishable in a screenshot; the only way to know the meter reads the
 * surface is to hand it two different surfaces and compare what renders.
 *
 * So this drives the production derivation with the production view and
 * compares the rendered output — the lit bars, the edge pattern, the label —
 * rather than the value the derivation returned.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { pluginCatalogId } from "@/a2ui/aleph-catalog-v09";
import { Block } from "@/components/Block";
import { blockProvenance } from "@/components/Board";

/** Render a block for a surface created under `catalogId`, as the Board does. */
function blockFor(catalogId: string | undefined, hasSurface = true) {
  const { band, trust } = blockProvenance(catalogId, hasSurface);
  const { container } = render(
    <Block title="pane" band={band} trust={trust}>
      <div />
    </Block>,
  );
  const block = container.querySelector<HTMLElement>('[data-testid="block"]');
  if (!block) throw new Error("Block did not render");
  const meter = block.querySelector<HTMLElement>("footer span[aria-label]");
  if (!meter) throw new Error("Block rendered no trust meter");
  return {
    trust: block.dataset.trust,
    band: block.dataset.band,
    label: meter.getAttribute("aria-label"),
    // The bars are the at-a-glance reading; count the LIT ones. The unlit ones
    // are still in the DOM, so "how many bars are there" is always four and
    // asserting that would pass whatever the meter says.
    lit: [...meter.querySelectorAll<HTMLElement>("span")].filter(
      (bar) => bar.style.background === "var(--accent)",
    ).length,
    edge: block.querySelector<HTMLElement>("span[title]")?.style.background ?? "",
  };
}

// A real plugin id, built by the same function that builds them in production.
// A literal `"aleph://plugin/atlas@1"` here would keep passing after the id
// shape changed, which is precisely the drift `pluginCatalogId` exists to stop.
const CORE = "aleph://v1";
const PLUGIN = pluginCatalogId("atlas", 1);

describe("the trust meter reads the surface it is framing", () => {
  it("lights a different number of bars for a plugin surface than for a core one", () => {
    const core = blockFor(CORE);
    const plugin = blockFor(PLUGIN);
    expect(core.lit).not.toBe(plugin.lit);
    expect(core.lit).toBe(4);
    expect(plugin.lit).toBe(1);
  });

  it("labels the two differently, for anyone who cannot count bars", () => {
    // Trust is deliberately shown as weight rather than hue, which is only
    // useful if the accessible name changes with it too.
    expect(blockFor(CORE).label).toBe("trust: signed");
    expect(blockFor(PLUGIN).label).toBe("trust: agent");
  });

  it("draws a solid edge for core and a broken one for a plugin", () => {
    expect(blockFor(CORE).edge).not.toContain("repeating-linear-gradient");
    expect(blockFor(PLUGIN).edge).toContain("repeating-linear-gradient");
  });

  it("marks the band third-party only when a plugin drew the surface", () => {
    expect(blockFor(CORE).band).toBe("declarative");
    expect(blockFor(PLUGIN).band).toBe("third-party");
  });

  it("shows a block with no surface yet as unverified, not as signed", () => {
    // The window between opening a pane and its first frame. Full trust there
    // would mean the meter reads `signed` for a surface that has not said what
    // it is — including the surface that turns out to be a plugin's.
    const pending = blockFor(undefined, false);
    expect(pending.trust).toBe("unverified");
    expect(pending.lit).toBe(0);
    expect(pending.trust).not.toBe(blockFor(CORE).trust);
  });
});

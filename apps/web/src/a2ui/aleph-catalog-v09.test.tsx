/**
 * One catalog per plugin, merged without collisions. WS-A3b, browser half.
 *
 * The defect is a `Map.set`. Two plugins both defining `Chart` means the second
 * one merged replaces the first: `new Catalog` does not mind, TypeScript does
 * not mind, `MessageProcessor` resolves the surviving one, and the browser
 * draws the wrong card against the right data. Nothing throws, so nothing in
 * review or in CI sees it.
 *
 * Every assertion here reads the REAL `Catalog` objects production builds —
 * `catalog.components` is `@a2ui/web_core`'s own map, the one the renderer
 * resolves against — never a shape this file made up.
 */
import { z as z3 } from "zod3";
import { createComponentImplementation } from "@a2ui/react/v0_9";
import { CommonSchemas } from "@a2ui/web_core/v0_9";
import { describe, expect, it } from "vitest";

import {
  ALEPH_CORE_CATALOG_ID,
  ALEPH_V09_CATALOG_ID,
  buildAlephCatalog,
  buildAlephCatalogs,
  buildAlephChatCatalog,
  isPluginCatalog,
  pluginCatalogId,
  registerPluginComponents,
} from "@/a2ui/aleph-catalog-v09";

/** A plugin-authored component, built exactly the way a real one would be. */
function impl(name: string, label: string) {
  return createComponentImplementation(
    { name, schema: z3.object({ title: CommonSchemas.DynamicString.optional() }) },
    () => <span data-testid={label}>{label}</span>,
  );
}

describe("catalog ids", () => {
  it("puts the major in a plugin's id, so @1 and @2 are different catalogs", () => {
    // The whole reason a surface created before an upgrade keeps painting.
    // Without the major, an upgrade is a destructive replace of every live
    // surface and nothing reports it.
    expect(pluginCatalogId("atlas", 1)).toBe("aleph://plugin/atlas@1");
    expect(pluginCatalogId("atlas", 2)).toBe("aleph://plugin/atlas@2");
    expect(pluginCatalogId("atlas", 1)).not.toBe(pluginCatalogId("atlas", 2));
  });

  it("registers core under both its name and its legacy alias", () => {
    // `apps/copilot-runtime/src/server.ts` still stamps `aleph://v1` as
    // `defaultCatalogId`. A rename in one process and not the other is how
    // every live surface starts answering `Catalog not found`.
    const ids = buildAlephCatalogs().map((c) => c.id);
    expect(ids).toContain(ALEPH_CORE_CATALOG_ID);
    expect(ids).toContain(ALEPH_V09_CATALOG_ID);
  });

  it("carries the basic-catalog FUNCTIONS, which one copy of this once dropped", () => {
    // The original defect: two builders under one id, one passing `[]` for
    // functions. A surface binding `formatDate` rendered in a pane and threw
    // `Function not found in catalog` in chat.
    const core = buildAlephCatalog();
    expect(core.functions.size).toBeGreaterThan(0);
    expect(core.functions.has("formatDate")).toBe(true);
    expect(buildAlephChatCatalog().functions.has("formatDate")).toBe(true);
  });
});

describe("two plugins may each define a component called Chart", () => {
  it("keeps each plugin's Chart in its own catalog", () => {
    const alpha = pluginCatalogId("alpha", 1);
    const beta = pluginCatalogId("beta", 1);
    const alphaChart = impl("Chart", "alpha-chart");
    const betaChart = impl("Chart", "beta-chart");
    registerPluginComponents(alpha, [alphaChart]);
    registerPluginComponents(beta, [betaChart]);

    const byId = new Map(
      buildAlephCatalogs([
        { catalogId: alpha, plugin: "alpha", major: 1 },
        { catalogId: beta, plugin: "beta", major: 1 },
      ]).map((c) => [c.id, c]),
    );

    // The assertion that matters is IDENTITY, not presence. A merge that
    // ignored ids would leave both catalogs resolving whichever Chart was
    // registered last, and a `toBeDefined()` here would pass.
    expect(byId.get(alpha)?.components.get("Chart")).toBe(alphaChart);
    expect(byId.get(beta)?.components.get("Chart")).toBe(betaChart);
    // Core is untouched by either.
    expect(byId.get(ALEPH_CORE_CATALOG_ID)?.components.has("Chart")).toBe(false);
  });

  it("still gives each plugin catalog the core primitives it needs to lay out", () => {
    const id = pluginCatalogId("gamma", 1);
    registerPluginComponents(id, [impl("Gauge", "gamma-gauge")]);
    const catalog = buildAlephCatalogs([{ catalogId: id, plugin: "gamma" }]).find(
      (c) => c.id === id,
    );
    expect(catalog?.components.has("Column")).toBe(true);
    expect(catalog?.components.has("ClaimCard")).toBe(true);
    expect(catalog?.components.has("Gauge")).toBe(true);
  });
});

describe("collisions are refused, naming both sides", () => {
  it("refuses a plugin component that would shadow a core one", () => {
    expect(() =>
      registerPluginComponents(pluginCatalogId("shadow", 1), [impl("ClaimCard", "no")]),
    ).toThrow(/ClaimCard/);
    expect(() =>
      registerPluginComponents(pluginCatalogId("shadow", 1), [impl("ClaimCard", "no")]),
    ).toThrow(new RegExp(ALEPH_CORE_CATALOG_ID.replace(/[/@]/g, "\\$&")));
  });

  it("refuses two descriptors claiming one catalog id", () => {
    const id = pluginCatalogId("delta", 1);
    expect(() =>
      buildAlephCatalogs([
        { catalogId: id, plugin: "delta" },
        { catalogId: id, plugin: "delta-fork" },
      ]),
    ).toThrow(/delta-fork/);
  });

  it("refuses a plugin descriptor that claims core's own id", () => {
    expect(() =>
      buildAlephCatalogs([{ catalogId: ALEPH_CORE_CATALOG_ID, plugin: "impostor" }]),
    ).toThrow(/impostor/);
  });
});

describe("the chat merge", () => {
  it("drops a component two plugins both claim rather than letting one win", () => {
    // Chat takes ONE catalog, so the array has to be flattened — and a flatten
    // is exactly where the overwrite the per-plugin ids removed comes back.
    // Rendering epsilon's Chart against zeta's data is worse than not rendering
    // Chart, so neither is merged.
    const epsilon = pluginCatalogId("epsilon", 1);
    const zeta = pluginCatalogId("zeta", 1);
    registerPluginComponents(epsilon, [impl("Chart", "e"), impl("Ledger", "e-ledger")]);
    registerPluginComponents(zeta, [impl("Chart", "z")]);

    const chat = buildAlephChatCatalog([
      { catalogId: epsilon, plugin: "epsilon" },
      { catalogId: zeta, plugin: "zeta" },
    ]);
    expect(chat.components.has("Chart")).toBe(false);
    expect(chat.components.has("Ledger")).toBe(true); // uncontested, still there
    expect(chat.components.has("ClaimCard")).toBe(true); // core, always there
  });

  it("answers to the id the copilot-runtime bridge actually stamps", () => {
    // `defaultCatalogId: "aleph://v1"` in apps/copilot-runtime/src/server.ts. A
    // chat catalog under any other id resolves to `Catalog not found` on the
    // agent's first surface.
    expect(buildAlephChatCatalog().id).toBe(ALEPH_V09_CATALOG_ID);
  });
});

/**
 * Who drew this surface — the one signal the Board's trust meter has.
 *
 * `Block` shows band and trust on every block always, and both were hardcoded
 * `declarative` / `signed`, so a core surface and a plugin's said exactly the
 * same thing about their own provenance. The distinction now comes from the
 * catalog a surface was created under, which means the recogniser and the
 * builder must agree — they are two separate literals of the same string, and
 * this is what stops them drifting. The builder's template is inline on purpose
 * (`check-single-catalog.sh` greps it for the major version).
 */
describe("plugin provenance", () => {
  it("recognises the ids its own builder produces, at any major", () => {
    expect(isPluginCatalog(pluginCatalogId("atlas"))).toBe(true);
    expect(isPluginCatalog(pluginCatalogId("atlas", 2))).toBe(true);
    expect(isPluginCatalog(pluginCatalogId("dispute-queue", 11))).toBe(true);
  });

  it("does not mistake a core catalog for a plugin's", () => {
    // Getting this backwards marks every surface the product itself draws as
    // third-party, which is the same failure as marking none of them.
    expect(isPluginCatalog(ALEPH_CORE_CATALOG_ID)).toBe(false);
    expect(isPluginCatalog(ALEPH_V09_CATALOG_ID)).toBe(false);
    expect(isPluginCatalog(undefined)).toBe(false);
    expect(isPluginCatalog("")).toBe(false);
  });
});

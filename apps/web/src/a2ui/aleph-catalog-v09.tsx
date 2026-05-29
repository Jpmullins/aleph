/**
 * Aleph's shared A2UI v0.9 catalog (Wave 4).
 *
 * This is the single source of truth for Aleph's domain cards under the
 * upstream `@a2ui` v0_9 stack. Both the right panel and the Live chat consume
 * the same `ReactComponentImplementation`s defined here, so a card looks and
 * behaves identically whether an agent emits it inline in chat or the panel
 * renders it from a server surface.
 *
 * ---------------------------------------------------------------------------
 * PROVEN PATTERN (Wave 4 Task 1 spike — verified end-to-end with HypothesisCard)
 * ---------------------------------------------------------------------------
 *
 * 1. Component API (name + zod schema). Props use `CommonSchemas.*`
 *    (`DynamicString`/`DynamicNumber`/`Action`/...). A `Dynamic*` prop may be
 *    EITHER a literal (`"under_investigation"`) OR a data binding
 *    (`{ path: "/h/confidence" }`); the Generic Binder resolves it to a plain
 *    value before the view renders.
 *
 *      export const HypothesisCardApi = {
 *        name: "HypothesisCard",
 *        schema: z.object({ title: CommonSchemas.DynamicString, ... }),
 *      };
 *
 * 2. Implementation via `createComponentImplementation(api, FC)`. The FC
 *    receives `{ props }` where `props` is the RESOLVED, strongly-typed object
 *    (literals + bindings already collapsed to values). We adapt to Aleph's
 *    existing card views, which take `{ component: { type, id, props }, onAction }`.
 *
 * 3. Catalog: `new Catalog(id, [impls], [funcs?])` (see `aleph-surface-v09`
 *    / SpikePanel for how this is fed to a `MessageProcessor` and rendered with
 *    `<A2uiSurface surface={...} />`). The basic-catalog primitives
 *    (`Text`/`Column`/`Row`/...) come from `basicCatalog.components` and are
 *    merged in so agents can compose layout around Aleph's domain cards.
 *
 * Message shape (server-to-client, v0.9), props passthrough INLINE on the
 * component object keyed by schema prop name:
 *
 *      { version: "v0.9", createSurface: { surfaceId, catalogId } }
 *      { version: "v0.9", updateComponents: { surfaceId, components: [
 *          { id: "card", component: "HypothesisCard",
 *            title: "…", confidence: { path: "/h/confidence" }, ... } ] } }
 *      { version: "v0.9", updateDataModel: { surfaceId, path: "/",
 *          value: { h: { confidence: "under_investigation" } } } }
 *
 * Reactive mutation: `surface.dataModel.set("/h/confidence", "likely")` updates
 * the bound prop in place and re-renders the card (Preact signals under the hood).
 *
 * ---------------------------------------------------------------------------
 * ZOD VERSION (the biggest Wave-4 risk — RESOLVED, and NOT the way first assumed)
 * ---------------------------------------------------------------------------
 * The v0_9 Generic Binder (`@a2ui/web_core`'s `scrapeSchemaBehavior`) inspects
 * a card's schema using zod *v3* internals — `_def.typeName === 'ZodObject'`,
 * `_def.options`, `_def.shape()` — to classify each prop as DYNAMIC / ACTION /
 * STRUCTURAL / STATIC. `@a2ui/web_core` does NOT take zod as a peer; it bundles
 * its OWN `zod@3.25.76` (a direct dependency), and `CommonSchemas` is built from
 * that v3 instance.
 *
 * The app, meanwhile, resolves `zod@4.4.3`. In zod v4 the schema internals were
 * rewritten: `_def.typeName` is `undefined` (it is now `_def.type === "object"`/
 * "union"), `_def.options` moved, and `shape` is a property not a method. So if
 * the OUTER `z.object({...})` wrapping the `CommonSchemas` props is built with
 * the app's v4 zod, `scrapeSchemaBehavior` fails to recognize it as an object,
 * collapses the whole schema to `STATIC`, and every prop — including the
 * `confidence` data-binding `{ path: "/h/confidence" }` — is passed through
 * UNRESOLVED to the view. The raw `{path}` object then reaches React and throws
 * "Objects are not valid as a React child". (This is exactly what the first
 * spike build did; the original note claiming "CommonSchemas imports cleanly
 * under zod 4" conflated the v0_9 binder with CopilotKit's separate renderer.)
 *
 * FIX: build the card's prop schema with zod v3 (matching the binder), via the
 * `zod3` alias (`npm:zod@3.25.76` in package.json), and use `z3.object(...)` to
 * wrap the `CommonSchemas.*` props. The binder matches A2UI primitives purely by
 * `_def.typeName` string (see `scrapeSchemaBehavior` — "structural matching ... to
 * avoid dual-module instanceof issues"), so `zod3` need not be the byte-identical
 * module instance `@a2ui/web_core` bundles — any zod@3.25.76 produces the same
 * `_def.typeName` ('ZodObject'/'ZodUnion'/'ZodOptional') and the same DYNAMIC
 * classification. `scrapeSchemaBehavior` then sees `confidence` as DYNAMIC and the
 * binder collapses the binding to its data-model value before the view renders.
 * (No type cast needed for the schema now — it is genuinely the v3 shape
 * `createComponentImplementation` expects. CopilotKit's `copilot-catalog.tsx` is
 * unaffected: it uses CopilotKit's own renderer/binder, not the v0_9 Generic
 * Binder, so its zod-v4 schemas are read by a different code path.)
 *
 * IMPLICATION FOR TASKS 3+: every one of the 17 Aleph cards migrated to the v0_9
 * shared catalog MUST declare its prop schema with `z3` (zod v3) + `CommonSchemas`,
 * never the app's default `zod` import. A v4 `z.object` silently disables binding.
 */
import { z as z3 } from "zod3";
import { createComponentImplementation } from "@a2ui/react/v0_9";
import { CommonSchemas } from "@a2ui/web_core/v0_9";

import { HypothesisCard as HypothesisCardView } from "./components/HypothesisCard";

/**
 * Component API: name + zod prop schema. Built with zod v3 (`z3`) so the v0_9
 * Generic Binder's `scrapeSchemaBehavior` recognizes each `CommonSchemas.*` prop
 * (DynamicString/DynamicNumber) as a bindable DYNAMIC value. `.optional()` is the
 * v3 optional wrapper, which the binder unwraps before classifying — so optional
 * dynamic props bind correctly too.
 */
export const HypothesisCardApi = {
  name: "HypothesisCard",
  schema: z3.object({
    hypothesis_id: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString,
    confidence: CommonSchemas.DynamicString.optional(),
    evidence_count: CommonSchemas.DynamicNumber.optional(),
  }),
};

/**
 * React implementation. `props` is the binder-resolved, plain-value object
 * (bindings + literals already collapsed). We forward it into Aleph's existing
 * HypothesisCard view. Card actions are intentionally a no-op in the spike;
 * the full migration wires them to Aleph's ActionRouter (as the panel does).
 */
export const HypothesisCardImpl = createComponentImplementation(
  HypothesisCardApi,
  ({ props }: { props: Record<string, unknown> }) => (
    <HypothesisCardView
      component={{ type: "HypothesisCard", id: "h", props }}
      onAction={() => {}}
    />
  ),
);

/**
 * Aleph's shared A2UI v0.9 catalog (Wave 4).
 *
 * This is the single source of truth for Aleph's domain cards + surfaces under
 * the upstream `@a2ui` v0_9 stack. Both the right panel and the Live chat
 * consume the same `ReactComponentImplementation`s defined here, so a card looks
 * and behaves identically whether an agent emits it inline in chat or the panel
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
 * 2. Implementation via `createComponentImplementation(api, FC)`. The FC
 *    receives `{ props }` where `props` is the RESOLVED, strongly-typed object
 *    (literals + bindings already collapsed to values). We adapt to Aleph's
 *    existing card/surface views, which take `{ component: { type, id, props },
 *    onAction }`.
 *
 * 3. Catalog: `new Catalog(id, [impls], [funcs?])`. The basic-catalog primitives
 *    (`Text`/`Column`/`Row`/...) are merged in (see `A2UISurfaceView`) so agents
 *    can compose layout around Aleph's domain cards. This single catalog is the
 *    one source of truth for BOTH surfaces: the right panel feeds it server
 *    surfaces via `A2UISurfaceView`, and the Live chat hands it to CopilotKit's
 *    `createA2UIMessageRenderer` (`lib/copilot.tsx`) for agent-emitted cards.
 *
 * 4. Actions. A card's user action (`approve`/`reject`/`open`/`navigate_wiki`/
 *    `submit_form`/...) is routed through Aleph's ActionRouter
 *    (`POST /v1/projects/{id}/cards/actions`) — NOT dispatched back into the
 *    agent's A2UI stream. The `adapt()` wrapper below reads `projectId`/`surface`
 *    from the `SurfaceProvider` context that wraps the rendered tree (the right
 *    panel via `A2UISurfaceView`, the chat via `CopilotChatSurface`), so a gated
 *    ApprovalCard's "Approve" executes the proposed change and refreshes the live
 *    surfaces. This is the Wave 6 chat behavior, now shared by both surfaces.
 *
 * ---------------------------------------------------------------------------
 * ZOD VERSION — the #1 Wave-4 failure mode (RESOLVED)
 * ---------------------------------------------------------------------------
 * The v0_9 Generic Binder (`@a2ui/web_core`'s `scrapeSchemaBehavior`) inspects a
 * card's schema using zod *v3* internals (`_def.typeName === 'ZodObject'`, etc.)
 * to classify each prop as DYNAMIC / ACTION / STRUCTURAL / STATIC. The app
 * resolves `zod@4`, whose internals are incompatible — a v4 `z.object` is NOT
 * recognized as an object, the whole schema collapses to STATIC, and every prop
 * (including data bindings `{ path }`) is passed through UNRESOLVED, crashing
 * React with "Objects are not valid as a React child".
 *
 * FIX (load-bearing, applies to EVERY card below): build the prop schema with
 * zod v3 via the `zod3` alias (`npm:zod@3.25.76` in package.json) and
 * `z3.object({...})` wrapping `CommonSchemas.*` props. `.optional()` is the v3
 * optional wrapper, which the binder unwraps before classifying.
 *
 * STRUCTURAL vs DYNAMIC: a `Dynamic*`/`Action` prop is a BINDABLE scalar — the
 * binder resolves `{ path }` against the data model. A complex literal that is
 * passed whole (a Vega-Lite spec, table columns/rows, graph nodes/edges, geo
 * points, form field specs) is NOT a binding; type it as `z3.any()` /
 * `z3.array(z3.object({...}))` so it passes through verbatim as a literal.
 */
import { z as z3 } from "zod3";
import { basicCatalog, createComponentImplementation } from "@a2ui/react/v0_9";
import { Catalog, CommonSchemas } from "@a2ui/web_core/v0_9";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { useSurface } from "./surface-context";
import { api } from "@/lib/api";
import { useWorkspaceUI } from "@/lib/workspace-ui";

import { ApprovalCard as ApprovalCardView } from "./components/ApprovalCard";
import { ArtifactCard as ArtifactCardView } from "./components/ArtifactCard";
import { ArtifactsSurface as ArtifactsSurfaceView } from "./components/ArtifactsSurface";
import { BriefsSurface as BriefsSurfaceView } from "./components/BriefsSurface";
import { GroundingSurface as GroundingSurfaceView } from "./components/GroundingSurface";
import { InspectorSurface as InspectorSurfaceView } from "./components/InspectorSurface";
import { ChartCard as ChartCardView } from "./components/ChartCard";
import { ClaimCard as ClaimCardView } from "./components/ClaimCard";
import { DiffCard as DiffCardView } from "./components/DiffCard";
import { FindingCard as FindingCardView } from "./components/FindingCard";
import { FormCard as FormCardView } from "./components/FormCard";
import { HtmlDocCard as HtmlDocCardView } from "./components/HtmlDocCard";
import { HtmlFrameCard as HtmlFrameCardView } from "./components/HtmlFrameCard";
import { ImageCard as ImageCardView } from "./components/ImageCard";
import { HypothesesSurface as HypothesesSurfaceView } from "./components/HypothesesSurface";
import { HypothesisCard as HypothesisCardView } from "./components/HypothesisCard";
import { NoteEditorCard as NoteEditorCardView } from "./components/NoteEditorCard";
import { NotesSurface as NotesSurfaceView } from "./components/NotesSurface";
import { SettingsSurface as SettingsSurfaceView } from "./components/SettingsSurface";
import { SourceCard as SourceCardView } from "./components/SourceCard";
import { TableCard as TableCardView } from "./components/TableCard";
import { WikiPageCard as WikiPageCardView } from "./components/WikiPageCard";
import { WikiSurface as WikiSurfaceView } from "./components/WikiSurface";

import type { A2UIComponent, ComponentName } from "./catalog";

/**
 * Adapter: the v0_9 binder hands us a RESOLVED plain-value `props` object.
 *
 * Exported so a test can drive the real dispatch → navigate → pane path with
 * the same wrapper every card in this file is built from. `props` is the only
 * thing a test supplies, and it is exactly what the binder produces; the
 * mutation, the navigate handling and the workspace call are all production. Our
 * existing views take `{ component: { type, id, props }, onAction }`. This
 * helper wraps a view as a v0_9 React impl and routes the view's `onAction`
 * through Aleph's ActionRouter (`POST /v1/projects/{id}/cards/actions`) using
 * the `projectId`/`surface` from the `SurfaceProvider` context — see the module
 * doc (#4). On success it invalidates the live-surface queries so the executed
 * action is reflected across the workspace, exactly as the Wave 6 chat path did.
 */
type ViewProps = {
  component: A2UIComponent;
  onAction: (action: string, params: Record<string, unknown>) => void;
};

export function adapt(
  name: ComponentName,
  View: (p: ViewProps) => React.ReactNode,
  idHint: string,
) {
  return function AlephCardImpl({ props }: { props: Record<string, unknown> }) {
    const { projectId, surface } = useSurface();
    const qc = useQueryClient();
    const { setActiveSurface, setOpenPageId, openPane } = useWorkspaceUI();

    const action = useMutation({
      mutationFn: async ({
        actionName,
        params,
      }: {
        actionName: string;
        params: Record<string, unknown>;
      }) =>
        api.post<{
          result?: {
            navigate?: {
              tab?: string;
              page_id?: string;
              /** The pane's DECLARED params, by name — `{claim_id}` for grounding. */
              params?: Record<string, string>;
            };
          };
        }>(`/v1/projects/${projectId}/cards/actions`, {
          surface_kind: surface,
          action_kind: actionName,
          target_id: (params.target_id as string | undefined) ?? null,
          target_kind: (params.target_kind as string | undefined) ?? null,
          params,
        }),
      onSuccess: (out) => {
        // Mirror the right panel: refresh the live surfaces (Briefs/Artifacts/
        // Hypotheses/Wiki/Notes) so the executed action is reflected there.
        qc.invalidateQueries({ queryKey: ["surface", projectId] });
        qc.invalidateQueries({ queryKey: ["artifacts", projectId] });
        qc.invalidateQueries({ queryKey: ["hypotheses", projectId] });
        // `open` actions resolve to a workspace location — actually go there.
        const nav = out?.result?.navigate;
        // No client-side validation of the name: the server produced this
        // navigate target and the server owns the set of surfaces.
        if (nav?.tab) {
          // `page_id` is the older, single-purpose form of the same thing and
          // still arrives on its own from `navigate_wiki`. Fold it in.
          const params: Record<string, string> = { ...(nav.params ?? {}) };
          if (nav.page_id) {
            params.page_id = nav.page_id;
            setOpenPageId(nav.page_id);
          }
          if (Object.keys(params).length > 0) {
            // A parameterised pane must be opened WITH its parameter. The pane
            // id is the wire `surfaceId` and carries the params, so opening the
            // bare kind and hoping is how "Open claim" showed an empty
            // Grounding surface — indistinguishable from a claim with no
            // evidence, which is the one thing that pane exists to tell apart.
            openPane(nav.tab, { params });
          } else {
            setActiveSurface(nav.tab);
          }
        }
      },
    });

    // Children, if present, ride on the resolved props (`children` is a
    // STRUCTURAL prop typed below). Aleph views read `component.children`.
    const { children, ...rest } = props as {
      children?: A2UIComponent[];
    } & Record<string, unknown>;
    return (
      <View
        component={{
          type: name,
          id: idHint,
          props: rest,
          children: Array.isArray(children) ? children : undefined,
        }}
        onAction={(actionName, params) => {
          // Resolves TRUE/FALSE rather than rejecting: most cards do not await
          // this, and a rejecting promise nobody holds is an unhandled
          // rejection in the console of an app that is working correctly.
          const settled = new Promise<boolean>((resolve) => {
            action.mutate(
              { actionName, params },
              { onSuccess: () => resolve(true), onError: () => resolve(false) },
            );
          });
          return settled;
        }}
      />
    );
  };
}

// ---------------------------------------------------------------------------
// Cards
// ---------------------------------------------------------------------------

export const HypothesisCardApi = {
  name: "HypothesisCard",
  schema: z3.object({
    hypothesis_id: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString,
    confidence: CommonSchemas.DynamicString.optional(),
    evidence_count: CommonSchemas.DynamicNumber.optional(),
    open_action: CommonSchemas.Action.optional(),
  }),
};
export const HypothesisCardImpl = createComponentImplementation(
  HypothesisCardApi,
  adapt("HypothesisCard", HypothesisCardView, "h"),
);

export const ClaimCardApi = {
  name: "ClaimCard",
  schema: z3.object({
    claim_id: CommonSchemas.DynamicString,
    text: CommonSchemas.DynamicString,
    confidence: CommonSchemas.DynamicString,
    // `citations` is a whole-object literal (array of {marker, source_short_id}),
    // not a bindable scalar — passthrough.
    citations: z3.array(z3.any()).optional(),
    open_action: CommonSchemas.Action.optional(),
  }),
};
export const ClaimCardImpl = createComponentImplementation(
  ClaimCardApi,
  adapt("ClaimCard", ClaimCardView, "claim"),
);

export const SourceCardApi = {
  name: "SourceCard",
  schema: z3.object({
    source_id: CommonSchemas.DynamicString,
    short_id: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString,
    url: CommonSchemas.DynamicString.optional(),
    status: CommonSchemas.DynamicString,
    normalized_preview: CommonSchemas.DynamicString.optional(),
    // WP-6: true when the source has been retracted.
    retracted: CommonSchemas.DynamicBoolean.optional(),
    open_action: CommonSchemas.Action.optional(),
    navigate_wiki_action: CommonSchemas.Action.optional(),
  }),
};
export const SourceCardImpl = createComponentImplementation(
  SourceCardApi,
  adapt("SourceCard", SourceCardView, "source"),
);

export const ArtifactCardApi = {
  name: "ArtifactCard",
  schema: z3.object({
    artifact_id: CommonSchemas.DynamicString,
    short_id: CommonSchemas.DynamicString.optional(),
    title: CommonSchemas.DynamicString,
    artifact_kind: CommonSchemas.DynamicString,
    status: CommonSchemas.DynamicString,
    // WP-6: live-computed drift flag (an upstream page has a newer revision).
    drifted: CommonSchemas.DynamicBoolean.optional(),
    open_action: CommonSchemas.Action.optional(),
  }),
};
export const ArtifactCardImpl = createComponentImplementation(
  ArtifactCardApi,
  adapt("ArtifactCard", ArtifactCardView, "artifact"),
);

export const ChartCardApi = {
  name: "ChartCard",
  schema: z3.object({
    title: CommonSchemas.DynamicString.optional(),
    chart_id: CommonSchemas.DynamicString.optional(),
    // Vega-Lite spec is a whole-object literal — passthrough.
    vega_lite_spec: z3.any().optional(),
    // No `chart_url`, no `artifact_version_id`, no `open_action`. The first two
    // said the card could reach a URL it is forbidden to fetch (see the loader
    // in ChartCard.tsx) and nothing ever sent either; `open_action` was
    // REQUIRED of every producer and the view fires no Open button, because the
    // ActionRouter's `open` has no branch that could route a chart anywhere.
  }),
};
export const ChartCardImpl = createComponentImplementation(
  ChartCardApi,
  adapt("ChartCard", ChartCardView, "chart"),
);

// WP-4c sandbox viz pipeline — cards reference code_runner artifacts by URI.
export const ImageCardApi = {
  name: "ImageCard",
  schema: z3.object({
    src: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString.optional(),
    alt: CommonSchemas.DynamicString.optional(),
  }),
};
export const ImageCardImpl = createComponentImplementation(
  ImageCardApi,
  adapt("ImageCard", ImageCardView, "image"),
);

export const HtmlFrameCardApi = {
  name: "HtmlFrameCard",
  schema: z3.object({
    src: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString.optional(),
  }),
};
export const HtmlFrameCardImpl = createComponentImplementation(
  HtmlFrameCardApi,
  adapt("HtmlFrameCard", HtmlFrameCardView, "html-frame"),
);

export const TableCardApi = {
  name: "TableCard",
  schema: z3.object({
    dataset_version_id: CommonSchemas.DynamicString.optional(),
    title: CommonSchemas.DynamicString.optional(),
    // columns/rows are whole-object literals — passthrough.
    columns: z3.array(z3.any()).optional(),
    rows: z3.array(z3.any()).optional(),
    _placeholder: CommonSchemas.DynamicBoolean.optional(),
  }),
};
export const TableCardImpl = createComponentImplementation(
  TableCardApi,
  adapt("TableCard", TableCardView, "table"),
);

export const ApprovalCardApi = {
  name: "ApprovalCard",
  schema: z3.object({
    target_id: CommonSchemas.DynamicString,
    target_kind: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString,
    summary: CommonSchemas.DynamicString,
    severity: CommonSchemas.DynamicString.optional(),
    // evidence_refs is a whole-object literal — passthrough.
    evidence_refs: z3.array(z3.any()).optional(),
    approve_action: CommonSchemas.Action.optional(),
    reject_action: CommonSchemas.Action.optional(),
  }),
};
export const ApprovalCardImpl = createComponentImplementation(
  ApprovalCardApi,
  adapt("ApprovalCard", ApprovalCardView, "approval"),
);

export const FindingCardApi = {
  name: "FindingCard",
  schema: z3.object({
    finding_id: CommonSchemas.DynamicString,
    severity: CommonSchemas.DynamicString,
    kind: CommonSchemas.DynamicString,
    summary: CommonSchemas.DynamicString,
    evidence_refs: z3.array(z3.any()).optional(),
    open_action: CommonSchemas.Action.optional(),
    approve_action: CommonSchemas.Action.optional(),
    reject_action: CommonSchemas.Action.optional(),
  }),
};
export const FindingCardImpl = createComponentImplementation(
  FindingCardApi,
  adapt("FindingCard", FindingCardView, "finding"),
);

export const FormCardApi = {
  name: "FormCard",
  schema: z3.object({
    form_id: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString,
    // field specs are whole-object literals — passthrough.
    fields: z3.array(z3.any()),
    submit_action: CommonSchemas.Action.optional(),
  }),
};
export const FormCardImpl = createComponentImplementation(
  FormCardApi,
  adapt("FormCard", FormCardView, "form"),
);

export const DiffCardApi = {
  name: "DiffCard",
  schema: z3.object({
    from_revision_id: CommonSchemas.DynamicString,
    to_revision_id: CommonSchemas.DynamicString,
    page_id: CommonSchemas.DynamicString,
    from_body_md: CommonSchemas.DynamicString.optional(),
    to_body_md: CommonSchemas.DynamicString.optional(),
  }),
};
export const DiffCardImpl = createComponentImplementation(
  DiffCardApi,
  adapt("DiffCard", DiffCardView, "diff"),
);

// ---------------------------------------------------------------------------
// Reader / editor tier (WP-4 sub-spec b). Rich reader + note editor + compiled
// HTML doc. All data arrives via bound props; mutations route through onAction.
// The array/object props (claims/citations/wikilinks_out/page_meta) are
// whole-value literals — passthrough (z3.any), not bindable scalars.
// ---------------------------------------------------------------------------
export const WikiPageCardApi = {
  name: "WikiPageCard",
  schema: z3.object({
    page_id: CommonSchemas.DynamicString.optional(),
    body_md: CommonSchemas.DynamicString.optional(),
    claims: z3.array(z3.any()).optional(),
    citations: z3.array(z3.any()).optional(),
    wikilinks_out: z3.array(z3.any()).optional(),
    page_meta: z3.any().optional(),
    html_url: CommonSchemas.DynamicString.optional(),
    // WP-6: true when the page has ≥1 retracted-confidence claim.
    retracted: CommonSchemas.DynamicBoolean.optional(),
    derived: CommonSchemas.DynamicBoolean.optional(),
    read_only: CommonSchemas.DynamicBoolean.optional(),
    navigate_wiki_action: CommonSchemas.Action.optional(),
    approve_action: CommonSchemas.Action.optional(),
    reject_action: CommonSchemas.Action.optional(),
    repair_links_action: CommonSchemas.Action.optional(),
  }),
};
export const WikiPageCardImpl = createComponentImplementation(
  WikiPageCardApi,
  adapt("WikiPageCard", WikiPageCardView, "wiki-page"),
);

export const NoteEditorCardApi = {
  name: "NoteEditorCard",
  schema: z3.object({
    note_id: CommonSchemas.DynamicString.optional(),
    section_id: CommonSchemas.DynamicString.optional(),
    title: CommonSchemas.DynamicString.optional(),
    body_md: CommonSchemas.DynamicString.optional(),
    edit_action: CommonSchemas.Action.optional(),
    rename_action: CommonSchemas.Action.optional(),
    promote_action: CommonSchemas.Action.optional(),
  }),
};
export const NoteEditorCardImpl = createComponentImplementation(
  NoteEditorCardApi,
  adapt("NoteEditorCard", NoteEditorCardView, "note-editor"),
);

export const HtmlDocCardApi = {
  name: "HtmlDocCard",
  schema: z3.object({
    src: CommonSchemas.DynamicString,
    title: CommonSchemas.DynamicString.optional(),
    derived: CommonSchemas.DynamicBoolean.optional(),
    read_only: CommonSchemas.DynamicBoolean.optional(),
  }),
};
export const HtmlDocCardImpl = createComponentImplementation(
  HtmlDocCardApi,
  adapt("HtmlDocCard", HtmlDocCardView, "html-doc"),
);

// ---------------------------------------------------------------------------
// Surfaces — the rich, self-contained tab views. Each is registered as a single
// v0_9 component; the view fetches its own data (pages/artifacts/notes/...) via
// React Query and reads `projectId`/`surface` from the `SurfaceProvider` that
// `A2UISurfaceView` wraps around the surface tree. Their props are STRUCTURAL
// literals (filters, current-selection ids, child card lists) — passthrough.
// ---------------------------------------------------------------------------

// WP-4: the four canonical tabs are data-bound. Each data prop is a
// `DynamicValue` (a union that includes `{ path }`), so a `{ path: "/pages" }`
// binding resolves the WHOLE array/object from the surface data model before
// the view renders — and a per-path `updateDataModel` delta re-resolves only
// the changed prop. The views render exclusively from these resolved props.
export const WikiSurfaceApi = {
  name: "WikiSurface",
  schema: z3.object({
    pages: CommonSchemas.DynamicValue.optional(),
    open: CommonSchemas.DynamicValue.optional(),
    // The binder resolves ONLY the props declared here — an undeclared one is
    // dropped silently, so the view sees `undefined` while the server is
    // sending the data. A producer without its matching declaration is the
    // write-path-with-no-read-path failure in miniature: the surface payload
    // carried `categories` and `health` correctly and the wiki rendered as if
    // the project had no categories at all.
    categories: CommonSchemas.DynamicValue.optional(),
    health: CommonSchemas.DynamicValue.optional(),
  }),
};
export const WikiSurfaceImpl = createComponentImplementation(
  WikiSurfaceApi,
  adapt("WikiSurface", WikiSurfaceView, "wiki"),
);

export const ArtifactsSurfaceApi = {
  name: "ArtifactsSurface",
  schema: z3.object({
    sources: CommonSchemas.DynamicValue.optional(),
    artifacts: CommonSchemas.DynamicValue.optional(),
  }),
};
export const ArtifactsSurfaceImpl = createComponentImplementation(
  ArtifactsSurfaceApi,
  adapt("ArtifactsSurface", ArtifactsSurfaceView, "artifacts"),
);

export const NotesSurfaceApi = {
  name: "NotesSurface",
  schema: z3.object({
    notes: CommonSchemas.DynamicValue.optional(),
  }),
};
export const NotesSurfaceImpl = createComponentImplementation(
  NotesSurfaceApi,
  adapt("NotesSurface", NotesSurfaceView, "notes"),
);

export const HypothesesSurfaceApi = {
  name: "HypothesesSurface",
  schema: z3.object({
    items: CommonSchemas.DynamicValue.optional(),
    ach: CommonSchemas.DynamicValue.optional(),
  }),
};
export const HypothesesSurfaceImpl = createComponentImplementation(
  HypothesesSurfaceApi,
  adapt("HypothesesSurface", HypothesesSurfaceView, "hypotheses"),
);

export const BriefsSurfaceApi = {
  name: "BriefsSurface",
  schema: z3.object({
    badge_count: CommonSchemas.DynamicNumber.optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
export const BriefsSurfaceImpl = createComponentImplementation(
  BriefsSurfaceApi,
  adapt("BriefsSurface", BriefsSurfaceView, "briefs"),
);

export const GroundingSurfaceApi = {
  name: "GroundingSurface",
  schema: z3.object({
    // `CommonSchemas.DynamicValue`, not `z3.any()`. See the note on
    // `SettingsSurfaceApi` below: the binder classifies a `z3.any()` prop as
    // STATIC and hands the view the literal `{ path: "/claim" }` instead of
    // resolving it, so this pane rendered nothing at all. `z3.any()` here was
    // found by WS-B1 while debugging the identical mistake in a new surface —
    // and `check-surface-bindings.sh` is green on it, because that sweep asks
    // whether the prop is DECLARED, not whether it is declared BINDABLE.
    claim: CommonSchemas.DynamicValue.optional(),
    groundings: CommonSchemas.DynamicValue.optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
export const GroundingSurfaceImpl = createComponentImplementation(
  GroundingSurfaceApi,
  adapt("GroundingSurface", GroundingSurfaceView, "grounding"),
);

export const InspectorSurfaceApi = {
  name: "InspectorSurface",
  // Every prop the producer binds is declared here. `check-surface-bindings.sh`
  // exists because the wiki surface once shipped ten categories and a health
  // summary the client never declared: the binder resolves only declared props,
  // so the SSE payload was correct, the view read `undefined`, and nothing
  // raised.
  //
  // The three data props are `CommonSchemas.DynamicValue`, not `z3.any()`, and
  // that distinction is what makes the pane render at all. A `z3.any()` prop is
  // classified STATIC by the v0_9 binder and passed through VERBATIM, so
  // `runs` arrived as the object `{ path: "/runs" }`, `runs.length === 0` was
  // false, `runs.map` threw, and React unmounted the Inspector every time it
  // was opened — with the server sending a correct payload on every frame.
  schema: z3.object({
    runs: CommonSchemas.DynamicValue.optional(),
    selected: CommonSchemas.DynamicValue.optional(),
    events: CommonSchemas.DynamicValue.optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
export const InspectorSurfaceImpl = createComponentImplementation(
  InspectorSurfaceApi,
  adapt("InspectorSurface", InspectorSurfaceView, "inspector"),
);

/**
 * WS-B1. Settings is a PANE, and the drawer is gone.
 *
 * Two props, and the second is the whole design: `sections` is an ordered list
 * the SERVER composes, so `settings`, `logs`, `notifications` and `profile` are
 * four values of one component rather than four components. `Drawers.tsx` had a
 * React function per section, which is exactly why a plugin's settings had
 * nowhere to land.
 *
 * Both are declared here because the binder resolves ONLY declared props —
 * `check-surface-bindings.sh` exists because the wiki surface once shipped ten
 * categories the client never declared and rendered as though the project had
 * none.
 */
export const SettingsSurfaceApi = {
  name: "SettingsSurface",
  schema: z3.object({
    title: CommonSchemas.DynamicValue.optional(),
    // `CommonSchemas.DynamicValue`, NOT `z3.any()`, and the difference is the
    // whole pane. The v0_9 binder classifies each declared prop from its schema:
    // a `Dynamic*` is BINDABLE and `{ path: "/sections" }` is resolved against
    // the data model, while `z3.any()` is STATIC and passed through VERBATIM.
    // Declared as `z3.any()` this prop arrived at the view as the literal object
    // `{ path: "/sections" }` — every `updateDataModel` correct on the wire,
    // every section computed, and `sections.map is not a function` in the
    // console. Measured in a browser, not reasoned about.
    sections: CommonSchemas.DynamicValue.optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
export const SettingsSurfaceImpl = createComponentImplementation(
  SettingsSurfaceApi,
  adapt("SettingsSurface", SettingsSurfaceView, "settings"),
);

/**
 * Every Aleph domain impl (13 cards + 5 surfaces). `buildAlephCatalog` below
 * merges these with the basic-catalog primitives.
 */
export const ALEPH_CARD_IMPLS = [
  // cards
  HypothesisCardImpl,
  ClaimCardImpl,
  SourceCardImpl,
  ArtifactCardImpl,
  ChartCardImpl,
  ImageCardImpl,
  HtmlFrameCardImpl,
  TableCardImpl,
  ApprovalCardImpl,
  FindingCardImpl,
  FormCardImpl,
  DiffCardImpl,
  // reader / editor tier (WP-4b)
  WikiPageCardImpl,
  NoteEditorCardImpl,
  HtmlDocCardImpl,
  // surfaces
  WikiSurfaceImpl,
  ArtifactsSurfaceImpl,
  NotesSurfaceImpl,
  HypothesesSurfaceImpl,
  BriefsSurfaceImpl,
  GroundingSurfaceImpl,
  InspectorSurfaceImpl,
  SettingsSurfaceImpl,
];

/**
 * The catalog ids the backend's `createSurface.catalogId` references.
 *
 * ONE declaration each, in this module and nowhere else. `ALEPH_V09_CATALOG_ID`
 * and `buildAlephCatalog` used to exist twice — once in `A2UISurfaceView.tsx`
 * (functions: `[]`) and once in `SurfaceStreamProvider.tsx` (functions: all 25
 * basic ones) — under the *same* id. A surface using `formatDate`, `equals` or
 * `openUrl` therefore rendered in a pane and threw
 * `Function not found in catalog 'aleph://v1'` in chat, because
 * `lib/copilot.tsx` built the chat renderer from the function-less copy. Two
 * catalogs claiming one identity is the same defect class this repo already
 * burned a work package on with three hand-maintained catalog copies.
 *
 * WS-A3b changes the rule from "one catalog" to "one catalog PER ID". A
 * renderer holds several named catalogs at once and `createSurface` says which
 * one it means (`MessageProcessor` looks the id up and throws
 * `Catalog not found` on a miss), so two plugins may each define a component
 * called `Chart` provided their catalogs have different ids. What must never
 * happen again is two catalogs claiming ONE id, which is a silent map
 * overwrite. `scripts/check-single-catalog.sh` fails the build on a catalog-id
 * declaration outside this module, a `new Catalog(` outside this module, two
 * declared ids with the same value, or the plugin-id template losing the part
 * that makes it unique.
 */
export const ALEPH_CORE_CATALOG_ID = "aleph://core@1";

/**
 * Core under the name it had before the convention existed.
 *
 * Kept, and registered as a real second catalog over the same components,
 * because two producers still stamp it: `apps/copilot-runtime/src/server.ts`
 * (`defaultCatalogId`) and `aleph_a2ui.components.surfaces`. Renaming an id in
 * one process and not the other is how every live surface starts answering
 * `Catalog not found` — and this alias is also the mechanism the `@<major>`
 * convention promises in general, made concrete: two ids, one contents, both
 * resolvable, no migration.
 */
export const ALEPH_V09_CATALOG_ID = "aleph://v1";

/**
 * A plugin's catalog id. The MAJOR is in the string on purpose.
 *
 * `aleph://plugin/atlas@1` and `aleph://plugin/atlas@2` are different strings,
 * therefore different catalogs, therefore they coexist in one processor array
 * — so a surface created before an upgrade keeps painting against the catalog
 * it named. Drop the `@${major}` and an upgrade becomes a destructive replace
 * of every live surface, with no error anywhere.
 */
export function pluginCatalogId(name: string, major = 1) {
  return `aleph://plugin/${name}@${major}`;
}

/** The shape `pluginCatalogId` builds, as a recogniser.
 *
 *  A second literal of the same string, and the pair is pinned by
 *  `aleph-catalog-v09.test.tsx` — `isPluginCatalog(pluginCatalogId("atlas", 2))`
 *  must be true, so the two cannot drift apart in a commit. The builder's
 *  template is deliberately left inline: `check-single-catalog.sh` greps it to
 *  assert the MAJOR is in the id, and an id that lost its major would silently
 *  replace the catalog every already-open surface is painting with. */
const PLUGIN_CATALOG_ID = /^aleph:\/\/plugin\/[^/@]+@\d+$/;

/** Did a plugin draw this surface, or did the product? */
export function isPluginCatalog(catalogId: string | undefined): boolean {
  return Boolean(catalogId && PLUGIN_CATALOG_ID.test(catalogId));
}

/** What the server says exists: `GET /v1/projects/{id}/catalogs`. */
export interface PluginCatalogDescriptor {
  catalogId: string;
  plugin?: string | null;
  major?: number | null;
}

type AlephImpl = (typeof ALEPH_CARD_IMPLS)[number];

/**
 * Component implementations a plugin contributes to the browser, by catalog id.
 *
 * Empty today, and deliberately present: no plugin can yet ship React code, so
 * every plugin catalog is core under a plugin-scoped id. That is not a
 * placeholder — it is the isolation boundary doing its job. A plugin's settings
 * surface names `aleph://plugin/<name>@<major>`, so disabling the plugin
 * removes the catalog and the surface stops resolving instead of quietly
 * painting against core. `PaneRegistry.extend` exists on the server for the
 * same reason: the next thing that needs this should find a working door rather
 * than build one.
 */
const PLUGIN_IMPLS = new Map<string, AlephImpl[]>();

export function registerPluginComponents(catalogId: string, impls: AlephImpl[]) {
  const core = new Set(CORE_COMPONENT_NAMES);
  const shadowed = impls.map((i) => i.name).filter((n) => core.has(n));
  if (shadowed.length > 0) {
    // Named on both sides. "duplicate component" would send the author to diff
    // two catalogs by hand.
    throw new Error(
      `${catalogId} defines ${shadowed.join(", ")}, which ${ALEPH_CORE_CATALOG_ID} ` +
        `already defines. A catalog is a name-to-component map, so the second ` +
        `registration would replace the first with nothing reporting it.`,
    );
  }
  PLUGIN_IMPLS.set(catalogId, impls);
}

/** Every component name core can draw. */
export const CORE_COMPONENT_NAMES: readonly string[] = [
  ...ALEPH_CARD_IMPLS.map((i) => i.name),
  ...[...basicCatalog.components.values()].map((i) => i.name),
];

function coreImpls() {
  return [...ALEPH_CARD_IMPLS, ...basicCatalog.components.values()];
}

function coreFunctions() {
  return [...basicCatalog.functions.values()];
}

/**
 * The core catalog: every Aleph domain impl, every basic-catalog primitive, and
 * every basic-catalog *function* — the last of which is the part that was
 * missing in chat. Pure config with no per-surface state, so callers are free to
 * `useMemo` it and hand it to as many throwaway processors as they like.
 *
 * `packages/aleph-a2ui/tools/extract_render_catalog.mjs` calls this by name to
 * derive what the agent is told, so the export is load-bearing beyond the app.
 */
export function buildAlephCatalog() {
  return new Catalog(ALEPH_CORE_CATALOG_ID, coreImpls(), coreFunctions());
}

/** Core again, under its legacy id. Same components, different name. */
export function buildAlephLegacyCatalog() {
  return new Catalog(ALEPH_V09_CATALOG_ID, coreImpls(), coreFunctions());
}

/**
 * The ARRAY the renderer holds: core, core's legacy alias, then one catalog per
 * enabled plugin. `MessageProcessor` takes a list and resolves `createSurface`
 * against it by id — Aleph was already on that code path and passed a list of
 * exactly one.
 *
 * Refuses two descriptors claiming one id. That is the failure the per-plugin
 * ids exist to prevent, and it is invisible without a check: `new Catalog` does
 * not mind, `MessageProcessor.find` takes the first, and the second plugin's
 * surfaces silently render the first plugin's components.
 */
export function buildAlephCatalogs(plugins: readonly PluginCatalogDescriptor[] = []) {
  const core = buildAlephCatalog();
  const legacy = buildAlephLegacyCatalog();
  const claimed = new Map<string, string>([
    [core.id, "core"],
    [legacy.id, "core (legacy alias)"],
  ]);
  const out = [core, legacy];

  for (const plugin of plugins) {
    const prior = claimed.get(plugin.catalogId);
    if (prior !== undefined) {
      throw new Error(
        `catalog id ${plugin.catalogId} is claimed by both ${prior} and ` +
          `${plugin.plugin ?? "an unnamed plugin"}. createSurface resolves an id to ` +
          `exactly one catalog, so the second would silently win.`,
      );
    }
    claimed.set(plugin.catalogId, plugin.plugin ?? plugin.catalogId);
    out.push(
      new Catalog(
        plugin.catalogId,
        [...coreImpls(), ...(PLUGIN_IMPLS.get(plugin.catalogId) ?? [])],
        coreFunctions(),
      ),
    );
  }
  return out;
}

/**
 * The single flat catalog the CHAT renderer accepts.
 *
 * `createA2UIMessageRenderer({ catalog })` takes one catalog, so chat is a
 * merge — and a merge is exactly where the silent overwrite the per-plugin ids
 * removed comes back. Isolation that holds in panes and fails in chat is not
 * isolation, so the same rule runs here: when two plugins claim one component
 * name, NEITHER is merged. Picking a winner by order is the original defect
 * wearing a policy, and chat drawing plugin A's card against plugin B's data is
 * worse than chat not drawing it.
 *
 * Mirrors `aleph_a2ui.plugin_catalogs.merge_for_chat`, which computes the same
 * answer server-side and reports the dropped names.
 */
export function buildAlephChatCatalog(plugins: readonly PluginCatalogDescriptor[] = []) {
  const claims = new Map<string, string[]>();
  for (const plugin of plugins) {
    for (const impl of PLUGIN_IMPLS.get(plugin.catalogId) ?? []) {
      claims.set(impl.name, [...(claims.get(impl.name) ?? []), plugin.catalogId]);
    }
  }
  const merged: AlephImpl[] = [];
  for (const plugin of plugins) {
    for (const impl of PLUGIN_IMPLS.get(plugin.catalogId) ?? []) {
      if ((claims.get(impl.name) ?? []).length === 1) merged.push(impl);
    }
  }
  return new Catalog(ALEPH_V09_CATALOG_ID, [...coreImpls(), ...merged], coreFunctions());
}

/** The concrete component-api type the shared catalog carries (React impls). */
export type AlephComponentApi =
  ReturnType<typeof buildAlephCatalog> extends Catalog<infer T> ? T : never;

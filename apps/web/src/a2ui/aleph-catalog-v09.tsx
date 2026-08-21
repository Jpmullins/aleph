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
import { SourceCard as SourceCardView } from "./components/SourceCard";
import { TableCard as TableCardView } from "./components/TableCard";
import { WikiPageCard as WikiPageCardView } from "./components/WikiPageCard";
import { WikiSurface as WikiSurfaceView } from "./components/WikiSurface";

import type { A2UIComponent, ComponentName } from "./catalog";

/**
 * Adapter: the v0_9 binder hands us a RESOLVED plain-value `props` object. Our
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

function adapt(
  name: ComponentName,
  View: (p: ViewProps) => React.ReactNode,
  idHint: string,
) {
  return function AlephCardImpl({ props }: { props: Record<string, unknown> }) {
    const { projectId, surface } = useSurface();
    const qc = useQueryClient();
    const { setActiveSurface, setOpenPageId } = useWorkspaceUI();

    const action = useMutation({
      mutationFn: async ({
        actionName,
        params,
      }: {
        actionName: string;
        params: Record<string, unknown>;
      }) =>
        api.post<{
          result?: { navigate?: { tab?: string; page_id?: string } };
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
          if (nav.page_id) setOpenPageId(nav.page_id);
          setActiveSurface(nav.tab);
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
        onAction={(actionName, params) => action.mutate({ actionName, params })}
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
    // Streaming-route URI vega-embed can load (WP-4c) — no self-fetch here.
    chart_url: CommonSchemas.DynamicString.optional(),
    artifact_version_id: CommonSchemas.DynamicString.optional(),
    // Vega-Lite spec is a whole-object literal — passthrough.
    vega_lite_spec: z3.any().optional(),
    open_action: CommonSchemas.Action.optional(),
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
    artifact_version_id: CommonSchemas.DynamicString.optional(),
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
    artifact_version_id: CommonSchemas.DynamicString.optional(),
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
    open_action: CommonSchemas.Action.optional(),
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
    open_action: CommonSchemas.Action.optional(),
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
    filters: z3.any().optional(),
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
    claim: z3.any().optional(),
    groundings: z3.any().optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
export const GroundingSurfaceImpl = createComponentImplementation(
  GroundingSurfaceApi,
  adapt("GroundingSurface", GroundingSurfaceView, "grounding"),
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
];

/**
 * The catalog id the backend's `createSurface.catalogId` references.
 *
 * ONE declaration, deliberately. This constant and `buildAlephCatalog` used to
 * exist twice — once in `A2UISurfaceView.tsx` (functions: `[]`) and once in
 * `SurfaceStreamProvider.tsx` (functions: all 25 basic ones) — under the *same*
 * id. A surface using `formatDate`, `equals` or `openUrl` therefore rendered in
 * a pane and threw `Function not found in catalog 'aleph://v1'` in chat, because
 * `lib/copilot.tsx` built the chat renderer from the function-less copy. Two
 * catalogs claiming one identity is the same defect class this repo already
 * burned a work package on with three hand-maintained catalog copies.
 *
 * `scripts/check-single-catalog.sh` fails the build if a second declaration or
 * a second `new Catalog(` reappears anywhere under `apps/web/src`.
 */
export const ALEPH_V09_CATALOG_ID = "aleph://v1";

/**
 * The one catalog: every Aleph domain impl, every basic-catalog primitive, and
 * every basic-catalog *function* — the last of which is the part that was
 * missing in chat. Pure config with no per-surface state, so callers are free to
 * `useMemo` it and hand it to as many throwaway processors as they like.
 */
export function buildAlephCatalog() {
  return new Catalog(
    ALEPH_V09_CATALOG_ID,
    [...ALEPH_CARD_IMPLS, ...basicCatalog.components.values()],
    [...basicCatalog.functions.values()],
  );
}

/** The concrete component-api type the shared catalog carries (React impls). */
export type AlephComponentApi =
  ReturnType<typeof buildAlephCatalog> extends Catalog<infer T> ? T : never;

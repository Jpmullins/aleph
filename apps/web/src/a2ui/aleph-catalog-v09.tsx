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
import { createComponentImplementation } from "@a2ui/react/v0_9";
import { CommonSchemas } from "@a2ui/web_core/v0_9";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { useSurface } from "./surface-context";
import { api } from "@/lib/api";
import { SURFACE_TABS, useWorkspaceUI, type SurfaceTab } from "@/lib/workspace-ui";

import { ApprovalCard as ApprovalCardView } from "./components/ApprovalCard";
import { ArtifactCard as ArtifactCardView } from "./components/ArtifactCard";
import { ArtifactsSurface as ArtifactsSurfaceView } from "./components/ArtifactsSurface";
import { BriefsSurface as BriefsSurfaceView } from "./components/BriefsSurface";
import { ChartCard as ChartCardView } from "./components/ChartCard";
import { ClaimCard as ClaimCardView } from "./components/ClaimCard";
import { DiffCard as DiffCardView } from "./components/DiffCard";
import { FindingCard as FindingCardView } from "./components/FindingCard";
import { FormCard as FormCardView } from "./components/FormCard";
import { GraphCard as GraphCardView } from "./components/GraphCard";
import { HypothesesSurface as HypothesesSurfaceView } from "./components/HypothesesSurface";
import { HypothesisCard as HypothesisCardView } from "./components/HypothesisCard";
import { MapCard as MapCardView } from "./components/MapCard";
import { NotebookCellCard as NotebookCellCardView } from "./components/NotebookCellCard";
import { NotesSurface as NotesSurfaceView } from "./components/NotesSurface";
import { SourceCard as SourceCardView } from "./components/SourceCard";
import { TableCard as TableCardView } from "./components/TableCard";
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
        if (nav?.tab && (SURFACE_TABS as readonly string[]).includes(nav.tab)) {
          if (nav.page_id) setOpenPageId(nav.page_id);
          setActiveSurface(nav.tab as SurfaceTab);
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
    dataset_version_id: CommonSchemas.DynamicString.optional(),
    title: CommonSchemas.DynamicString.optional(),
    chart_id: CommonSchemas.DynamicString.optional(),
    // Vega-Lite spec is a whole-object literal — passthrough.
    vega_lite_spec: z3.any().optional(),
    _placeholder: CommonSchemas.DynamicBoolean.optional(),
    open_action: CommonSchemas.Action.optional(),
  }),
};
export const ChartCardImpl = createComponentImplementation(
  ChartCardApi,
  adapt("ChartCard", ChartCardView, "chart"),
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

export const MapCardApi = {
  name: "MapCard",
  schema: z3.object({
    title: CommonSchemas.DynamicString.optional(),
    // points / center are whole-object literals — passthrough.
    points: z3.array(z3.any()).optional(),
    style_url: CommonSchemas.DynamicString.optional(),
    center: z3.any().optional(),
    zoom: CommonSchemas.DynamicNumber.optional(),
    _placeholder: CommonSchemas.DynamicBoolean.optional(),
  }),
};
export const MapCardImpl = createComponentImplementation(
  MapCardApi,
  adapt("MapCard", MapCardView, "map"),
);

export const GraphCardApi = {
  name: "GraphCard",
  schema: z3.object({
    title: CommonSchemas.DynamicString.optional(),
    // nodes / edges are whole-object literals — passthrough.
    nodes: z3.array(z3.any()).optional(),
    edges: z3.array(z3.any()).optional(),
    _placeholder: CommonSchemas.DynamicBoolean.optional(),
  }),
};
export const GraphCardImpl = createComponentImplementation(
  GraphCardApi,
  adapt("GraphCard", GraphCardView, "graph"),
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
    view_diff_action: CommonSchemas.Action.optional(),
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

export const NotebookCellCardApi = {
  name: "NotebookCellCard",
  schema: z3.object({
    section_id: CommonSchemas.DynamicString,
    body_md: CommonSchemas.DynamicString,
    ordinal: CommonSchemas.DynamicNumber,
    edit_action: CommonSchemas.Action.optional(),
  }),
};
export const NotebookCellCardImpl = createComponentImplementation(
  NotebookCellCardApi,
  adapt("NotebookCellCard", NotebookCellCardView, "section"),
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
    open_action: CommonSchemas.Action.optional(),
  }),
};
export const DiffCardImpl = createComponentImplementation(
  DiffCardApi,
  adapt("DiffCard", DiffCardView, "diff"),
);

// ---------------------------------------------------------------------------
// Surfaces — the rich, self-contained tab views. Each is registered as a single
// v0_9 component; the view fetches its own data (pages/artifacts/notes/...) via
// React Query and reads `projectId`/`surface` from the `SurfaceProvider` that
// `A2UISurfaceView` wraps around the surface tree. Their props are STRUCTURAL
// literals (filters, current-selection ids, child card lists) — passthrough.
// ---------------------------------------------------------------------------

export const WikiSurfaceApi = {
  name: "WikiSurface",
  schema: z3.object({
    current_page_id: CommonSchemas.DynamicString.optional(),
    view_mode: CommonSchemas.DynamicString.optional(),
    filters: z3.any().optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
export const WikiSurfaceImpl = createComponentImplementation(
  WikiSurfaceApi,
  adapt("WikiSurface", WikiSurfaceView, "wiki"),
);

export const ArtifactsSurfaceApi = {
  name: "ArtifactsSurface",
  schema: z3.object({
    current_artifact_id: CommonSchemas.DynamicString.optional(),
  }),
};
export const ArtifactsSurfaceImpl = createComponentImplementation(
  ArtifactsSurfaceApi,
  adapt("ArtifactsSurface", ArtifactsSurfaceView, "artifacts"),
);

export const NotesSurfaceApi = {
  name: "NotesSurface",
  schema: z3.object({
    current_note_id: CommonSchemas.DynamicString.optional(),
    current_section_id: CommonSchemas.DynamicString.optional(),
    children: z3.array(z3.any()).optional(),
  }),
};
export const NotesSurfaceImpl = createComponentImplementation(
  NotesSurfaceApi,
  adapt("NotesSurface", NotesSurfaceView, "notes"),
);

export const HypothesesSurfaceApi = {
  name: "HypothesesSurface",
  schema: z3.object({
    current_hypothesis_id: CommonSchemas.DynamicString.optional(),
    children: z3.array(z3.any()).optional(),
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

/**
 * Every Aleph domain impl (13 cards + 5 surfaces). The shared catalog
 * (`A2UISurfaceView.buildAlephCatalog`) merges these with the basic-catalog
 * primitives.
 */
export const ALEPH_CARD_IMPLS = [
  // cards
  HypothesisCardImpl,
  ClaimCardImpl,
  SourceCardImpl,
  ArtifactCardImpl,
  ChartCardImpl,
  TableCardImpl,
  MapCardImpl,
  GraphCardImpl,
  ApprovalCardImpl,
  FindingCardImpl,
  NotebookCellCardImpl,
  FormCardImpl,
  DiffCardImpl,
  // surfaces
  WikiSurfaceImpl,
  ArtifactsSurfaceImpl,
  NotesSurfaceImpl,
  HypothesesSurfaceImpl,
  BriefsSurfaceImpl,
];

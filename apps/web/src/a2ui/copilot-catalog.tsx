/**
 * Aleph's A2UI catalog for CopilotKit v2 (Wave 2).
 *
 * Bridges the agent's generative-UI tool calls to Aleph's existing card
 * components. CopilotKit's `createCatalog` takes two halves:
 *
 *   - **definitions** — a Zod schema + natural-language description per
 *     component. The descriptions are injected into the agent's system
 *     prompt (via the Node runtime's `a2ui.injectA2UITool`) so the LLM
 *     knows which card to emit and with what props.
 *   - **renderers** — a React component per definition. We adapt Aleph's
 *     existing cards (which take `{ component, onAction }`) to CopilotKit's
 *     renderer signature (`{ props, dispatch }`).
 *
 * The resulting catalog is handed to `createA2UIMessageRenderer({ catalog })`
 * in `lib/copilot.tsx`; the agent's streamed A2UI surfaces then render as the
 * real Aleph ChartCard / TableCard / ClaimCard / … inline in chat.
 *
 * `projectId` reaches the cards through the existing `SurfaceProvider`
 * context, which `CopilotChatSurface` wraps around `<CopilotChat>`.
 */
import { createCatalog } from "@copilotkit/a2ui-renderer";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";
import type { ComponentType } from "react";

import { useSurface } from "./register";
import { api } from "@/lib/api";

import { ApprovalCard } from "./components/ApprovalCard";
import { ArtifactCard } from "./components/ArtifactCard";
import { ChartCard } from "./components/ChartCard";
import { ClaimCard } from "./components/ClaimCard";
import { DiffCard } from "./components/DiffCard";
import { FindingCard } from "./components/FindingCard";
import { FormCard } from "./components/FormCard";
import { GraphCard } from "./components/GraphCard";
import { HypothesisCard } from "./components/HypothesisCard";
import { MapCard } from "./components/MapCard";
import { NotebookCellCard } from "./components/NotebookCellCard";
import { SourceCard } from "./components/SourceCard";
import { TableCard } from "./components/TableCard";

import type { A2UIComponent, ComponentName } from "./catalog";

const jsonObject = z.record(z.string(), z.unknown());

/**
 * Component definitions — Zod props + agent-facing descriptions. Only the
 * data cards an assistant would plausibly emit *inline in chat* are listed;
 * the five full-panel surfaces (WikiSurface, etc.) are driven by the right
 * panel, not generated mid-conversation.
 */
export const alephCatalogDefinitions = {
  ChartCard: {
    description:
      "A chart visualizing quantitative data. Provide a self-contained " +
      "Vega-Lite spec in `vega_lite_spec` with the data embedded under " +
      "`data.values` (an array of row objects). Use for comparisons, trends, " +
      "distributions, or benchmark numbers the analyst asks you to plot.",
    props: z.object({
      title: z.string().optional(),
      vega_lite_spec: jsonObject.optional(),
      dataset_version_id: z.string().optional(),
      chart_id: z.string().optional(),
    }),
  },
  TableCard: {
    description:
      "A sortable, filterable data table. Provide `columns` ([{name,label}]) " +
      "and `rows` (array of objects keyed by column name), or a " +
      "`dataset_version_id` to load an existing dataset.",
    props: z.object({
      title: z.string().optional(),
      columns: z
        .array(z.object({ name: z.string(), label: z.string().optional() }))
        .optional(),
      rows: z.array(jsonObject).optional(),
      dataset_version_id: z.string().optional(),
    }),
  },
  ClaimCard: {
    description:
      "A single factual claim with its confidence and citations. Use when " +
      "surfacing one well-scoped assertion grounded in the wiki.",
    props: z.object({
      claim_id: z.string(),
      text: z.string(),
      confidence: z.enum(["well-supported", "contested", "uncited", "initial"]),
      citations: z
        .array(
          z.object({
            marker: z.string(),
            source_short_id: z.string().nullable().optional(),
          }),
        )
        .optional(),
    }),
  },
  HypothesisCard: {
    description:
      "A research hypothesis with its current confidence and evidence count. " +
      "Use to surface a candidate explanation the analyst is weighing.",
    props: z.object({
      hypothesis_id: z.string(),
      title: z.string(),
      confidence: z.string().optional(),
      evidence_count: z.number().optional(),
    }),
  },
  FindingCard: {
    description:
      "A reviewer finding (contradiction, weak source, coverage gap) with a " +
      "severity. Use when reporting an issue the analyst can act on.",
    props: z.object({
      finding_id: z.string(),
      severity: z.enum(["info", "low", "medium", "high"]),
      kind: z.string(),
      summary: z.string(),
    }),
  },
  SourceCard: {
    description:
      "A reference to an ingested source with its processing status. Use when " +
      "pointing the analyst at a specific source document.",
    props: z.object({
      source_id: z.string(),
      short_id: z.string(),
      title: z.string(),
      url: z.string().nullable().optional(),
      status: z.string(),
    }),
  },
  ArtifactCard: {
    description:
      "A built product artifact (report/deck/source-pack) with its status. " +
      "Use after kicking off a build to show the analyst what is being produced.",
    props: z.object({
      artifact_id: z.string(),
      short_id: z.string().optional(),
      title: z.string(),
      artifact_kind: z.string(),
      status: z.string(),
    }),
  },
  ApprovalCard: {
    description:
      "A human-in-the-loop approval prompt for a proposed change (e.g. a " +
      "synthesis proposal or wiki edit). Renders approve/reject controls.",
    props: jsonObject,
  },
  DiffCard: {
    description: "A before/after text diff, e.g. a proposed wiki revision.",
    props: jsonObject,
  },
  GraphCard: {
    description:
      "A node-link graph (entities and their relationships, or a wikilink " +
      "neighborhood).",
    props: jsonObject,
  },
  MapCard: {
    description: "A geographic map with plotted points or regions.",
    props: jsonObject,
  },
  NotebookCellCard: {
    description: "A code/markdown notebook cell with its output.",
    props: jsonObject,
  },
  FormCard: {
    description:
      "An interactive form requesting structured input from the analyst.",
    props: jsonObject,
  },
} satisfies Record<string, { description: string; props: z.ZodTypeAny }>;

/** Aleph's card components keyed by name, all sharing the same render contract. */
type AlephCardComponent = ComponentType<{
  component: A2UIComponent;
  onAction: (action: string, params: Record<string, unknown>) => void;
}>;

const CARD_COMPONENTS: Record<keyof typeof alephCatalogDefinitions, AlephCardComponent> = {
  ChartCard,
  TableCard,
  ClaimCard,
  HypothesisCard,
  FindingCard,
  SourceCard,
  ArtifactCard,
  ApprovalCard,
  DiffCard,
  GraphCard,
  MapCard,
  NotebookCellCard,
  FormCard,
};

/**
 * Adapt one Aleph card to CopilotKit's renderer signature. The resolved A2UI
 * data-model `props` become the card's `component.props`; user actions on the
 * card are routed to Aleph's `ActionRouter` (`POST /v1/projects/{id}/cards/actions`)
 * exactly as the right panel does — *not* dispatched back into the agent's A2UI
 * stream. Every Aleph card action (approve/reject/open/navigate_wiki/submit_form/…)
 * is an ActionRouter action, so a gated ApprovalCard's "Approve" actually executes
 * the proposed change, and the right-panel surfaces refresh to reflect it.
 */
function adaptRenderer(type: ComponentName, Card: AlephCardComponent) {
  return function AlephCardRenderer({ props }: { props: Record<string, unknown> }) {
    const { projectId, surface } = useSurface();
    const qc = useQueryClient();

    const action = useMutation({
      mutationFn: async ({
        actionName,
        params,
      }: {
        actionName: string;
        params: Record<string, unknown>;
      }) =>
        api.post(`/v1/projects/${projectId}/cards/actions`, {
          surface_kind: surface,
          action_kind: actionName,
          target_id: (params.target_id as string | undefined) ?? null,
          target_kind: (params.target_kind as string | undefined) ?? null,
          params,
        }),
      onSuccess: () => {
        // Mirror the right panel: refresh the live surfaces (Briefs/Artifacts/
        // Hypotheses/Wiki/Notes) so the executed action is reflected there.
        qc.invalidateQueries({ queryKey: ["surface", projectId] });
        qc.invalidateQueries({ queryKey: ["artifacts", projectId] });
        qc.invalidateQueries({ queryKey: ["hypotheses", projectId] });
      },
    });

    const component: A2UIComponent = {
      type,
      id: typeof props.id === "string" ? props.id : type,
      props,
    };
    return (
      <Card
        component={component}
        onAction={(actionName, params) => action.mutate({ actionName, params })}
      />
    );
  };
}

const alephRenderers = Object.fromEntries(
  (Object.keys(alephCatalogDefinitions) as Array<keyof typeof alephCatalogDefinitions>).map(
    (name) => [name, adaptRenderer(name as ComponentName, CARD_COMPONENTS[name])],
  ),
);

/**
 * The Aleph A2UI catalog for CopilotKit. `includeBasicCatalog` merges A2UI's
 * primitive components (Text, Row, Column, Card, Button, …) so the agent can
 * compose layout around Aleph's domain cards.
 */
// Two casts at the library boundary:
//  - definitions: @copilotkit/a2ui-renderer's types are built against zod v3,
//    while Aleph uses zod v4 (incompatible `ZodObject` internals);
//  - renderers: our adapter takes loose `props`/`dispatch`, not the
//    per-component zod-inferred prop types `CatalogRenderers` expects.
// Both are sound at runtime — the schemas validate and the adapter forwards.
export const alephCatalog = createCatalog(
  alephCatalogDefinitions as never,
  alephRenderers as never,
  { catalogId: "aleph", includeBasicCatalog: true },
);

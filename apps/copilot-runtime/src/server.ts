/**
 * aleph-copilot-runtime — the Node CopilotKit runtime (Wave 2).
 *
 * Bridges the React app to the FastAPI AG-UI endpoint. This is where A2UI
 * tool injection lives (`a2ui.injectA2UITool`), per the CopilotKit v2 +
 * Deep Agents + A2UI integration. The browser talks to this service; this
 * service talks to aleph-api's `/copilotkit/agent/assistant` AG-UI endpoint.
 */
import { createServer } from "node:http";

import { HttpAgent } from "@ag-ui/client";
import { CopilotRuntime } from "@copilotkit/runtime/v2";
import { createCopilotNodeListener } from "@copilotkit/runtime/v2/node";

const PORT = Number(process.env.PORT ?? 4000);
const BASE_PATH = process.env.COPILOT_BASE_PATH ?? "/api/copilotkit";
// AG-UI endpoint exposed by aleph-api (compose network).
const AGENT_URL =
  process.env.ALEPH_AGENT_URL ?? "http://aleph-api:8000/copilotkit/agent/assistant";

/**
 * Aleph's A2UI catalog, declared to the agent so its injected `render_a2ui`
 * tool knows which domain cards it can generate and with what props, and —
 * critically — which `catalogId` to stamp on `createSurface`. We use the v0.9
 * inline-catalog format with `catalogId: "aleph://v1"` so the surfaces the agent
 * emits match the SHARED catalog the frontend registers (Wave 4 Task 4:
 * `apps/web/src/a2ui/aleph-catalog-v09.tsx` → `buildAlephCatalog`, id
 * `aleph://v1`). This is the same id the backend's server-emitted surfaces use
 * (`aleph_a2ui` builders), so chat and right panel share one catalog. The basic
 * A2UI primitives (Text, Row, Column, Card, Button, …) are merged into that
 * catalog on the frontend; these are Aleph's domain cards on top.
 */
const ALEPH_A2UI_CATALOG = {
  catalogId: "aleph://v1",
  components: {
    ChartCard: {
      description:
        "A chart visualizing quantitative data. Provide a self-contained " +
        "Vega-Lite spec in `vega_lite_spec` with data embedded under " +
        "`data.values` (array of row objects). Use for comparisons, trends, " +
        "distributions, or benchmark numbers the analyst asks you to plot.",
      props: {
        type: "object",
        properties: {
          title: { type: "string" },
          vega_lite_spec: { type: "object", description: "A complete Vega-Lite spec with embedded data." },
          dataset_version_id: { type: "string" },
        },
      },
    },
    TableCard: {
      description:
        "A sortable, filterable data table. Provide `columns` ([{name,label}]) " +
        "and `rows` (array of objects keyed by column name).",
      props: {
        type: "object",
        properties: {
          title: { type: "string" },
          columns: {
            type: "array",
            items: {
              type: "object",
              properties: { name: { type: "string" }, label: { type: "string" } },
              required: ["name"],
            },
          },
          rows: { type: "array", items: { type: "object" } },
        },
      },
    },
    ClaimCard: {
      description:
        "A single factual claim with its confidence and citations. Use when " +
        "surfacing one well-scoped assertion grounded in the wiki.",
      props: {
        type: "object",
        properties: {
          claim_id: { type: "string" },
          text: { type: "string" },
          confidence: { type: "string", enum: ["well-supported", "contested", "uncited", "initial"] },
          citations: {
            type: "array",
            items: {
              type: "object",
              properties: { marker: { type: "string" }, source_short_id: { type: "string" } },
            },
          },
        },
        required: ["claim_id", "text", "confidence"],
      },
    },
    HypothesisCard: {
      description: "A research hypothesis with its current confidence and evidence count.",
      props: {
        type: "object",
        properties: {
          hypothesis_id: { type: "string" },
          title: { type: "string" },
          confidence: { type: "string" },
          evidence_count: { type: "number" },
        },
        required: ["hypothesis_id", "title"],
      },
    },
    FindingCard: {
      description: "A reviewer finding (contradiction, weak source, coverage gap) with a severity.",
      props: {
        type: "object",
        properties: {
          finding_id: { type: "string" },
          severity: { type: "string", enum: ["info", "low", "medium", "high"] },
          kind: { type: "string" },
          summary: { type: "string" },
        },
        required: ["finding_id", "severity", "kind", "summary"],
      },
    },
    SourceCard: {
      description: "A reference to an ingested source with its processing status.",
      props: {
        type: "object",
        properties: {
          source_id: { type: "string" },
          short_id: { type: "string" },
          title: { type: "string" },
          url: { type: "string" },
          status: { type: "string" },
        },
        required: ["source_id", "short_id", "title", "status"],
      },
    },
    ArtifactCard: {
      description:
        "A built product artifact (report/deck/source-pack) with its status.",
      props: {
        type: "object",
        properties: {
          artifact_id: { type: "string" },
          short_id: { type: "string" },
          title: { type: "string" },
          artifact_kind: { type: "string" },
          status: { type: "string" },
        },
        required: ["artifact_id", "title", "artifact_kind", "status"],
      },
    },
    ApprovalCard: {
      description:
        "An approve/reject gate for a consequential action (an agent_action " +
        "build/connector change, a synthesis proposal, a review finding). Render " +
        "this when a tool result asks you to surface an approval — the analyst " +
        "clicks Approve/Reject and the ActionRouter executes. `target_id` + " +
        "`target_kind` come from the tool result; do not fabricate them.",
      props: {
        type: "object",
        properties: {
          target_id: { type: "string" },
          target_kind: { type: "string" },
          title: { type: "string" },
          summary: { type: "string" },
          severity: { type: "string", description: "info | low | medium | high" },
          evidence_refs: { type: "array", items: { type: "object" } },
          diff_card_id: { type: "string" },
        },
        required: ["target_id", "target_kind", "title", "summary"],
      },
    },
    FormCard: {
      description:
        "A dynamic input form. Use to collect structured input conversationally " +
        "(parameters, choices) instead of free text. `fields` is an array of field " +
        "specs ([{name,label,type,options?}]).",
      props: {
        type: "object",
        properties: {
          form_id: { type: "string" },
          title: { type: "string" },
          fields: { type: "array", items: { type: "object" } },
        },
        required: ["form_id", "title", "fields"],
      },
    },
    MapCard: {
      description:
        "A geographic map of point features. Provide `points` (array of " +
        "{lat,lng,label?} objects), optional `center` ({lat,lng}) and `zoom`.",
      props: {
        type: "object",
        properties: {
          title: { type: "string" },
          points: { type: "array", items: { type: "object" } },
          center: { type: "object" },
          zoom: { type: "number" },
          style_url: { type: "string" },
        },
      },
    },
    GraphCard: {
      description:
        "A node-link graph (entities + relationships). Provide `nodes` " +
        "([{id,label?}]) and `edges` ([{source,target,label?}]).",
      props: {
        type: "object",
        properties: {
          title: { type: "string" },
          nodes: { type: "array", items: { type: "object" } },
          edges: { type: "array", items: { type: "object" } },
        },
      },
    },
    NotebookCellCard: {
      description:
        "An editable analyst-note cell (markdown). Render when surfacing or " +
        "drafting a note section the analyst can edit in place.",
      props: {
        type: "object",
        properties: {
          section_id: { type: "string" },
          body_md: { type: "string" },
          ordinal: { type: "number" },
        },
        required: ["section_id", "body_md", "ordinal"],
      },
    },
    DiffCard: {
      description:
        "A revision diff for a wiki page (from_revision_id → to_revision_id). " +
        "Render when showing what changed between two wiki revisions.",
      props: {
        type: "object",
        properties: {
          from_revision_id: { type: "string" },
          to_revision_id: { type: "string" },
          page_id: { type: "string" },
        },
        required: ["from_revision_id", "to_revision_id", "page_id"],
      },
    },
  },
};

const runtime = new CopilotRuntime({
  agents: {
    assistant: new HttpAgent({ url: AGENT_URL }),
  },
  // Inject the render_a2ui tool so the agent can emit A2UI surfaces, and
  // declare Aleph's catalog so the agent knows what it can draw and stamps
  // `catalogId: "aleph"` on createSurface. The middleware intercepts A2UI
  // operations in the event stream; the frontend a2ui-renderer draws them
  // against the matching "aleph" catalog.
  a2ui: { injectA2UITool: true, schema: ALEPH_A2UI_CATALOG },
});

const listener = createCopilotNodeListener({
  runtime,
  basePath: BASE_PATH,
  cors: true,
});

createServer(listener).listen(PORT, "0.0.0.0", () => {
  // eslint-disable-next-line no-console
  console.log(
    `aleph-copilot-runtime listening on :${PORT}${BASE_PATH} → agent ${AGENT_URL}`,
  );
});

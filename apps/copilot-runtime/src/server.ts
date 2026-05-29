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
 * inline-catalog format with `catalogId: "aleph"` so the surfaces the agent
 * emits match the catalog the frontend registers (see
 * `apps/web/src/a2ui/copilot-catalog.tsx`, `catalogId: "aleph"`). The basic
 * A2UI primitives (Text, Row, Column, Card, Button, …) are merged into that
 * catalog on the frontend (`includeBasicCatalog: true`); these are Aleph's
 * domain cards on top.
 */
const ALEPH_A2UI_CATALOG = {
  catalogId: "aleph",
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

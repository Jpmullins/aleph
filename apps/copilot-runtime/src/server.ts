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

import { ALEPH_A2UI_CATALOG } from "./catalog.generated.js";

const PORT = Number(process.env.PORT ?? 4000);
const BASE_PATH = process.env.COPILOT_BASE_PATH ?? "/api/copilotkit";
// AG-UI endpoint exposed by aleph-api (compose network).
const AGENT_URL =
  process.env.ALEPH_AGENT_URL ?? "http://aleph-api:8000/copilotkit/agent/assistant";

/**
 * Aleph's A2UI catalog is GENERATED, not written here.
 *
 * It used to be a ~265-line object literal in this file, hand-mirrored from
 * `catalog.py` and `apps/web/src/a2ui/catalog.ts`. The three drifted: this copy
 * offered `ClaimCard.confidence: "initial"`, which nothing recognises, while
 * omitting `"retracted"` — so the agent could not emit the retracted state at
 * all. It now comes from `packages/aleph-a2ui/src/aleph_a2ui/catalog.json` via
 * `scripts/gen_catalog.py`, the same file the Python validator loads, so the
 * shapes the agent is told about and the shapes the server accepts cannot
 * disagree.
 *
 * The `catalogId` (`aleph://v1`) must match the catalog the frontend registers
 * (`apps/web/src/a2ui/aleph-catalog-v09.tsx` → `buildAlephCatalog`) and the one
 * the backend's `aleph_a2ui` builders stamp, so chat and right panel share one
 * catalog. A2UI's base primitives are carried in the generated file alongside
 * Aleph's domain cards.
 */

const runtime = new CopilotRuntime({
  agents: {
    assistant: new HttpAgent({ url: AGENT_URL }),
  },
  // Inject the render_a2ui tool so the agent can emit A2UI surfaces, and
  // declare Aleph's catalog so the agent knows what it can draw and stamps
  // `catalogId: "aleph"` on createSurface. The middleware intercepts A2UI
  // operations in the event stream; the frontend a2ui-renderer draws them
  // against the matching "aleph" catalog.
  // `defaultCatalogId` is critical: the A2UI middleware stamps this catalog id
  // on every STREAMED render_a2ui surface. Without it the middleware falls back
  // to the upstream basic-catalog id, which the frontend never registers — so
  // any agent-composed primitive surface (Column/Card/Text/…) fails with
  // "Catalog not found". Pointing it at "aleph://v1" — the shared catalog that
  // already merges the basic primitives in — makes those surfaces render.
  a2ui: {
    injectA2UITool: true,
    schema: ALEPH_A2UI_CATALOG,
    defaultCatalogId: "aleph://v1",
  },
});

const listener = createCopilotNodeListener({
  runtime,
  basePath: BASE_PATH,
  cors: true,
});

/**
 * `GET /health` — a readiness signal the orchestrator can actually use.
 *
 * There was none, so compose's healthcheck asked for `/health`, got a 404, and
 * marked a perfectly healthy process unhealthy forever. Everything else on this
 * server is POST-only AG-UI traffic, so there was nothing safe to probe.
 *
 * It reports the agent URL it will forward to, because the failure this service
 * actually has is pointing at a host that does not resolve — which looks
 * identical to working until someone opens the chat.
 */
createServer((req, res) => {
  if (req.method === "GET" && req.url === "/health") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ status: "ok", agent: AGENT_URL }));
    return;
  }
  listener(req, res);
}).listen(PORT, "0.0.0.0", () => {
  // eslint-disable-next-line no-console
  console.log(
    `aleph-copilot-runtime listening on :${PORT}${BASE_PATH} → agent ${AGENT_URL}`,
  );
});

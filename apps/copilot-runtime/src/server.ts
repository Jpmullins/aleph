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

/**
 * Origins allowed to drive the agent from a browser.
 *
 * This was `cors: true` — every origin, with credentials. Any page the user
 * had open could POST to :4000 and drive their assistant: read their project,
 * spend their tokens, write to their wiki. Nothing in the stack would have
 * recorded anything unusual, because the request is indistinguishable from the
 * real UI's.
 *
 * Configuration, not a constant, and it shares `ALEPH_CORS_ORIGINS` with the
 * API so the bridge and the thing it bridges to cannot drift into disagreeing
 * about who is allowed to talk to them.
 *
 * BE CLEAR ABOUT WHAT THIS DOES. CORS is enforced by browsers. It stops a
 * malicious PAGE from using someone's session; it does not stop `curl`, and it
 * never could. What narrows raw reachability is the publish address — see
 * `docker-compose.yml`, where this port is now bound to loopback instead of
 * every interface.
 */
const ALLOWED_ORIGINS = (process.env.ALEPH_CORS_ORIGINS ?? "http://localhost:5173")
  .split(",")
  .map((o) => o.trim())
  .filter(Boolean);

/**
 * Interface to bind to INSIDE the container.
 *
 * Stays `0.0.0.0` by default and that is correct: a container's published port
 * maps to the container's own interface, so binding to loopback here would make
 * the service unreachable from the host entirely. The narrowing that matters
 * happens at the publish address in `docker-compose.yml`.
 */
const BIND_ADDRESS = process.env.ALEPH_RUNTIME_BIND ?? "0.0.0.0";
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
    // No `fetch` override, and that is a MEASURED claim rather than an
    // assumption. Backlog D3 says this bridge "does not forward the user's
    // credential", and for this installed version that is false: the runtime
    // already copies the incoming `Authorization` header onto its call to the
    // agent. `scripts/_acceptance/runtime_bridge_probe.mjs` asserts it — a
    // hand-written forwarding shim was built here first, and removing it
    // changed nothing, which is how the redundancy was found.
    //
    // The real gap was on the OTHER side: the browser sent no credential at
    // all, because `CopilotKitProvider` was mounted without a `headers` prop.
    // Fixed in `apps/web/src/lib/copilot.tsx`.
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
  // The Open-Ended band. The agent gets a `generateSandboxedUi` tool and its
  // markup streams into a sealed iframe — opaque origin, no storage, no
  // same-origin fetch — so it can build a view nobody pre-designed.
  //
  // This is the escape hatch the catalog cannot provide. The catalog covers
  // what we anticipated; a researcher asking for a comparison nobody has drawn
  // before needs the agent to be able to draw it. CDN libraries load inside the
  // sandbox, so a real D3 or Chart.js visualisation is available without Aleph
  // shipping a chart type for every question.
  //
  // The frontend grants specific host functions on top of this — see
  // `apps/web/src/lib/copilot.tsx`. Generated code can call ONLY those.
  openGenerativeUI: true,
});

const listener = createCopilotNodeListener({
  runtime,
  basePath: BASE_PATH,
  cors: {
    origin: ALLOWED_ORIGINS,
    // Credentials are forwarded, so the origin list has to be exact — a
    // wildcard with credentials is the combination browsers refuse anyway.
    credentials: true,
  },
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
}).listen(PORT, BIND_ADDRESS, () => {
  // eslint-disable-next-line no-console
  console.log(
    `aleph-copilot-runtime listening on ${BIND_ADDRESS}:${PORT}${BASE_PATH} → ` +
      `agent ${AGENT_URL}; origins ${ALLOWED_ORIGINS.join(", ")}`,
  );
});

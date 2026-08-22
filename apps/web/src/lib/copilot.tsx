/**
 * CopilotKit v2 provider for Aleph (Wave 2).
 *
 * Wraps the app in `<CopilotKitProvider>` pointed at the Node CopilotRuntime
 * (`aleph-copilot-runtime`), which bridges to aleph-api's AG-UI Deep Agent
 * endpoint. Registers Aleph's A2UI catalog as an activity-message renderer so
 * the agent's streamed generative-UI surfaces render as real Aleph cards.
 *
 * Wave 4 Task 4: the chat consumes the SAME shared v0_9 catalog the right panel
 * uses (`buildAlephCatalog` over `aleph-catalog-v09`'s impls + the basic-catalog
 * primitives). The 13 cards + 5 surfaces are defined ONCE; both surfaces render
 * through identical `ReactComponentImplementation`s. CopilotKit's
 * `createA2UIMessageRenderer({ catalog })` feeds the catalog to its A2UI
 * `MessageProcessor`, which matches the agent's `createSurface.catalogId`
 * (`aleph://v1`, stamped by the copilot-runtime) to the shared catalog's id.
 *
 * The runtime URL is configured by `VITE_COPILOT_RUNTIME_URL`; in the local
 * compose stack the runtime is published on :4000.
 */
import {
  CopilotKitProvider,
  createA2UIMessageRenderer,
  a2uiDefaultTheme,
} from "@copilotkit/react-core/v2";
import { z } from "zod";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";

import { buildAlephChatCatalog } from "@/a2ui/aleph-catalog-v09";
import { api } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";

const RUNTIME_URL =
  (import.meta.env.VITE_COPILOT_RUNTIME_URL as string | undefined) ??
  "http://localhost:4000/api/copilotkit";

// Built once: maps the agent's A2UI surface messages to the shared Aleph
// catalog (same impls the right panel renders — cards defined once).
//
// `createA2UIMessageRenderer` takes ONE catalog, unlike `MessageProcessor`,
// which the reading region hands an array. So chat is a merge, and a merge is
// where the silent overwrite that per-plugin catalog ids removed comes back:
// `buildAlephChatCatalog` runs the same collision rule and drops a component
// name two plugins both claim rather than letting the last one registered win.
// Isolation that holds in panes and fails in chat is not isolation.
//
// It is built at module scope, so it carries core only. Wiring the project's
// plugin catalogs in here needs the renderer to be rebuilt per project, which
// `CopilotKitProvider` takes as a prop and would remount the whole chat on
// change; the pane path is where plugin surfaces live today.
const alephA2UIMessageRenderer = createA2UIMessageRenderer({
  theme: a2uiDefaultTheme,
  catalog: buildAlephChatCatalog(),
});

/**
 * The ARRAY has to be stable too, not just the renderer inside it.
 *
 * `renderActivityMessages={[alephA2UIMessageRenderer]}` built a fresh array on
 * every render of this provider, and CopilotKit answered
 * `renderActivityMessages must be a stable array` — an `error`-level console
 * message on every single workspace load, in a codebase whose browser suite had
 * no console listener at all, so nobody was told. Hoisted to module scope,
 * which is where the renderer already lived.
 */
const ACTIVITY_MESSAGE_RENDERERS = [alephA2UIMessageRenderer];

/**
 * How generated UI is allowed to look.
 *
 * The shipped default is shadcn-flavoured — rounded corners, white surfaces,
 * a coloured accent. Every one of those fights Aleph. Without overriding it,
 * the agent's own work is the one thing on screen that looks foreign, which
 * reads as "this part is not really ours".
 */
const ALEPH_DESIGN_SKILL = `Match Aleph's instrument aesthetic exactly.

- SQUARE corners everywhere. border-radius: 0. No exceptions.
- Read colours from the host page's CSS custom properties, never literals:
  background var(--surface-raised), page var(--surface-bg), text
  var(--text-primary), secondary text var(--text-secondary), muted
  var(--text-muted), hairlines 1px solid var(--border-muted), stronger edges
  var(--border-strong), the single accent var(--accent).
- Colour means STATE and nothing else: var(--state-good) for holding or
  confirmed, var(--state-bad) for contested or failed. Never colour anything
  decoratively — a palette where everything is tinted cannot say a thing is
  wrong.
- Type: "JetBrains Mono" for labels, units, ids and any figure; "Public Sans"
  for sentences; "Newsreader" serif only for long prose. Small sizes (10-13px)
  and tight spacing — this is an instrument, not a landing page.
- Hairline rules, no shadows, no gradients, no rounded pills, no emoji.
- Dense. Prefer more information at a smaller size over generous padding.`;

/**
 * What generated UI may reach.
 *
 * Sandboxed code has no same-origin access at all — no storage, no cookies, no
 * calling Aleph's API — so anything it needs must be handed over explicitly.
 * That makes this list a capability grant in the same sense the kernel means
 * it: the agent's markup can do exactly these two things and nothing else, and
 * each function's description is given to the agent so it knows what exists.
 *
 * Both are READ-ONLY on purpose. A generated view is a way of looking at the
 * corpus, not a way of changing it — writes go through the action router, which
 * records a ledger event in the same transaction. Nothing here can.
 */
const sandboxFunctions = [
  {
    name: "searchPages",
    description:
      "Search this project's pages and return matching hits with titles, " +
      "summaries and scores. Read-only. Use when the generated view should " +
      "show real content rather than illustrative values.",
    parameters: z.object({
      projectId: z.string().describe("The project to search within."),
      query: z.string().describe("Natural-language query."),
      topK: z.number().optional().describe("Max hits, default 8."),
    }),
    handler: async ({
      projectId,
      query,
      topK,
    }: {
      projectId: string;
      query: string;
      topK?: number;
    }) =>
      // POST /wiki/search — the endpoint that exists. Naming a bridge after an
      // endpoint that does not is how a grant ships looking correct and fails
      // only when the agent finally calls it.
      api.post(`/v1/projects/${projectId}/wiki/search`, {
        query,
        top_k: topK ?? 8,
      }),
  },
  {
    name: "getProjectPanes",
    description:
      "List the surfaces this project can open, with their ids and titles. " +
      "Read-only. Use to reference or link to a surface by its real id.",
    parameters: z.object({ projectId: z.string() }),
    handler: async ({ projectId }: { projectId: string }) =>
      api.get(`/v1/projects/${projectId}/panes`),
  },
];

export function AlephCopilotProvider({ children }: { children: ReactNode }) {
  // The chat path called the runtime with no credential at all: the provider
  // passed `runtimeUrl`, `renderActivityMessages` and `openGenerativeUI`, and
  // nothing else. The bridge then called the API anonymously, so the API saw
  // the BRIDGE rather than the person — and every other route in the app has
  // been authenticated since the `/copilotkit` exemption was removed.
  //
  // Held in state rather than read inline for one reason: `getAccessToken` is
  // async and this prop is not. Read once at mount and never refreshed, chat
  // would work until the token expired and then stop — a failure that looks
  // like the agent being broken rather than like a credential ageing out. In
  // `local` mode the token is a constant and never expires, so this costs
  // nothing today and is the part that would otherwise be missing later.
  //
  // NOTE: the installed CopilotKit types `headers` as
  // `Record<string, string> | Headers` — an OBJECT, not a function. A function
  // is silently accepted by JS and serialised to nothing.
  const [headers, setHeaders] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    void getAccessToken().then((token) => {
      if (!cancelled && token) {
        setHeaders({ Authorization: `Bearer ${token}` });
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <CopilotKitProvider
      runtimeUrl={RUNTIME_URL}
      headers={headers}
      renderActivityMessages={ACTIVITY_MESSAGE_RENDERERS}
      openGenerativeUI={{ sandboxFunctions, designSkill: ALEPH_DESIGN_SKILL }}
    >
      {children}
    </CopilotKitProvider>
  );
}

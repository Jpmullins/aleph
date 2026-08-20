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
import type { ReactNode } from "react";

import { buildAlephCatalog } from "@/a2ui/aleph-catalog-v09";

const RUNTIME_URL =
  (import.meta.env.VITE_COPILOT_RUNTIME_URL as string | undefined) ??
  "http://localhost:4000/api/copilotkit";

// Built once: maps the agent's A2UI surface messages to the shared Aleph
// catalog (same impls the right panel renders — cards defined once).
const alephA2UIMessageRenderer = createA2UIMessageRenderer({
  theme: a2uiDefaultTheme,
  catalog: buildAlephCatalog(),
});

export function AlephCopilotProvider({ children }: { children: ReactNode }) {
  return (
    <CopilotKitProvider
      runtimeUrl={RUNTIME_URL}
      renderActivityMessages={[alephA2UIMessageRenderer]}
    >
      {children}
    </CopilotKitProvider>
  );
}

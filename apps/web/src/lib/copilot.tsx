/**
 * CopilotKit v2 provider for Aleph (Wave 2).
 *
 * Wraps the app in `<CopilotKitProvider>` pointed at the Node CopilotRuntime
 * (`aleph-copilot-runtime`), which bridges to aleph-api's AG-UI Deep Agent
 * endpoint. Registers Aleph's A2UI catalog as an activity-message renderer so
 * the agent's streamed generative-UI surfaces render as real Aleph cards.
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

import { alephCatalog } from "@/a2ui/copilot-catalog";

const RUNTIME_URL =
  (import.meta.env.VITE_COPILOT_RUNTIME_URL as string | undefined) ??
  "http://localhost:4000/api/copilotkit";

// Built once: maps the agent's A2UI surface messages to Aleph's catalog.
const alephA2UIMessageRenderer = createA2UIMessageRenderer({
  theme: a2uiDefaultTheme,
  catalog: alephCatalog,
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

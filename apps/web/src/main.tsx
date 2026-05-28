import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { App } from "@/App";
import { AlephCopilotProvider } from "@/lib/copilot";
import "@copilotkit/react-core/v2/styles.css";
import "@/styles.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 30_000 },
  },
});

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("missing #root");
}

createRoot(rootEl).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AlephCopilotProvider>
        <App />
      </AlephCopilotProvider>
    </QueryClientProvider>
  </StrictMode>,
);

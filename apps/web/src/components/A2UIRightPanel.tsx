/**
 * One pane's content: an A2UI surface read out of the shared workspace stream.
 *
 * This used to own surface selection and open its own `EventSource`. Both moved:
 * selection lives in the pane model, and the connection is multiplexed at the
 * region level, so a pane is now purely a renderer for one `surfaceId`.
 *
 * Still no self-fetching — the CI-enforced rule holds, and now more strongly:
 * a pane has no transport of its own at all.
 */
import { A2uiSurface } from "@a2ui/react/v0_9";

import { useSurfaceStream } from "@/a2ui/SurfaceStreamProvider";
import { SurfaceProvider } from "@/a2ui/surface-context";
import type { Pane } from "@/lib/workspace-ui";

export function A2UIRightPanel({ projectId, pane }: { projectId: string; pane: Pane }) {
  const { surfaces, connected, error } = useSurfaceStream();
  const surface = surfaces.get(pane.id);

  return (
    <section
      className="flex h-full min-h-0 w-full min-w-0 flex-col bg-surface"
      aria-label={`${pane.kind} surface`}
      data-testid={`surface-${pane.kind.toLowerCase()}`}
    >
      <div className="min-h-0 flex-1 overflow-y-auto">
        {error ? (
          <p className="p-6 text-sm text-red-700" role="alert">
            This surface stopped updating: {error}
          </p>
        ) : surface ? (
          <SurfaceProvider projectId={projectId} surface={`${pane.kind}Surface`}>
            <A2uiSurface surface={surface} />
          </SurfaceProvider>
        ) : (
          <p className="p-6 text-sm text-ink-muted">
            {connected ? "Loading surface…" : "Reconnecting…"}
          </p>
        )}
      </div>
    </section>
  );
}

/**
 * The reading region — the stage.
 *
 * Renders the open panes tiled left-to-right. This is where the workspace
 * stopped being "five rooms you are always inside exactly one of" and became a
 * surface you put things onto: a page beside the source that grounds it, two
 * contradictory claims side by side, a generated chart beside the data it came
 * from. Comparison is the work, and a single slot cannot do it.
 *
 * Tiled rather than free-floating on purpose: comparison wants *alignment*
 * (a claim and its evidence should line up), tiling is deterministic enough to
 * put in a URL, and free positioning is an ongoing tax on the reader for no
 * gain in a workspace that is fundamentally columnar.
 */
import type { ReactNode } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";

import { SurfaceStreamProvider } from "@/a2ui/SurfaceStreamProvider";
import { A2UIRightPanel } from "@/components/A2UIRightPanel";
import { Icons } from "@/components/Icons";
import { type Pane, useWorkspaceUI } from "@/lib/workspace-ui";

function PaneShell({
  pane,
  projectId,
  focused,
  closable,
}: {
  pane: Pane;
  projectId: string;
  focused: boolean;
  closable: boolean;
}) {
  const { closePane, setFocusedPaneId } = useWorkspaceUI();

  return (
    <section
      className="flex h-full min-h-0 min-w-0 flex-col bg-surface"
      data-testid={`pane-${pane.id}`}
      data-focused={focused}
      aria-label={pane.title}
      onFocusCapture={() => setFocusedPaneId(pane.id)}
      onMouseDown={() => setFocusedPaneId(pane.id)}
    >
      <header
        className={
          "flex items-center gap-2 border-b px-3 py-1.5 " +
          (focused ? "border-accent bg-sunken" : "border-line bg-surface")
        }
      >
        <span className="text-[10px] font-bold uppercase tracking-[0.14em] text-ink-muted">
          {pane.kind}
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px] font-semibold text-ink" title={pane.title}>
          {pane.title}
        </span>
        {closable && (
          <button
            type="button"
            onClick={() => closePane(pane.id)}
            aria-label={`Close ${pane.title}`}
            title="Close pane"
            data-testid={`pane-close-${pane.id}`}
            className="grid h-6 w-6 place-items-center rounded text-ink-muted hover:bg-elevated hover:text-ink"
          >
            <Icons.close size={14} />
          </button>
        )}
      </header>
      <div className="min-h-0 flex-1 overflow-hidden">
        <A2UIRightPanel projectId={projectId} pane={pane} />
      </div>
    </section>
  );
}

export function ReadingRegion({ projectId }: { projectId: string }) {
  const { panes, focusedPaneId } = useWorkspaceUI();
  // Handles must be interleaved between panels, so build children explicitly.
  const children: ReactNode[] = [];
  panes.forEach((pane, i) => {
    if (i > 0) {
      children.push(
        <PanelResizeHandle
          key={`h-${pane.id}`}
          className="w-1 cursor-col-resize bg-line transition-colors hover:bg-accent"
        />,
      );
    }
    children.push(
      <Panel key={pane.id} id={pane.id} order={i} minSize={18} className="min-w-0">
        <PaneShell
          pane={pane}
          projectId={projectId}
          focused={pane.id === focusedPaneId}
          closable={panes.length > 1}
        />
      </Panel>,
    );
  });
  return (
    // One connection for every open pane. See SurfaceStreamProvider for why
    // this is both cheaper and more strictly ordered than one stream per pane.
    <SurfaceStreamProvider projectId={projectId} panes={panes.map((p) => p.id)}>
      <PanelGroup direction="horizontal" className="min-h-0 flex-1" data-testid="reading-region">
        {children}
      </PanelGroup>
    </SurfaceStreamProvider>
  );
}

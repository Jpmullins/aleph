/**
 * The Board — a spatial canvas the workbench arranges itself on.
 *
 * The old shell tiled panes left-to-right in fixed columns, which forced two
 * things that were wrong for generated content: everything had to be the same
 * height, and there was a hard ceiling on how many things could be visible.
 * Generated interfaces arrive at unknown sizes and in unknown numbers, and the
 * useful question is usually "how do these two relate", which a row of equal
 * columns cannot express.
 *
 * So: blocks sit where you put them, and the lines between them are drawn.
 * Provenance is a thread from a block to the block it came from — open the
 * grounding for a claim and you can see, without reading anything, which
 * surface it was drawn out of.
 *
 * Positions are deliberately local state rather than server-persisted. Where
 * you left a block is a property of the session you are in, and persisting it
 * would make the first thing a returning user sees an arrangement they built
 * for a question they have already answered.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { A2uiSurface } from "@a2ui/react/v0_9";

import { SurfaceStreamProvider, useSurfaceStream } from "@/a2ui/SurfaceStreamProvider";
import { SurfaceProvider } from "@/a2ui/surface-context";
import { Block, type BlockLifecycle } from "@/components/Block";
import { type Pane, useWorkspaceUI } from "@/lib/workspace-ui";

interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

const DEFAULT_W = 430;
const DEFAULT_H = 360;
const GAP = 26;

/** Lay a newly-opened block down where it does not cover the others. */
function autoPlace(index: number): Rect {
  const perRow = 3;
  return {
    x: GAP + (index % perRow) * (DEFAULT_W + GAP),
    y: GAP + Math.floor(index / perRow) * (DEFAULT_H + GAP),
    w: DEFAULT_W,
    h: DEFAULT_H,
  };
}

/** A cubic that leaves the source's right edge and arrives at the target's left. */
function thread(a: Rect, b: Rect): string {
  const x1 = a.x + a.w;
  const y1 = a.y + a.h / 2;
  const x2 = b.x;
  const y2 = b.y + b.h / 2;
  const dx = Math.max(40, Math.abs(x2 - x1) * 0.45);
  return `M${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function BoardCanvas({ projectId }: { projectId: string }) {
  const { panes, focusedPaneId, setFocusedPaneId, closePane } = useWorkspaceUI();
  const { surfaces, connected, error } = useSurfaceStream();

  const [rects, setRects] = useState<Record<string, Rect>>({});
  /** Which block each block was opened from — the thread's direction. */
  const [origin, setOrigin] = useState<Record<string, string>>({});
  const seen = useRef<Set<string>>(new Set());
  /**
   * The block that was focused BEFORE this render — which is the one a newly
   * opened block came out of.
   *
   * Assigning this during render (`ref.current = focusedPaneId`) is wrong and
   * silently produced zero threads: `openPane` adds the pane and focuses it in
   * the same update, so by the time the placement effect ran the ref already
   * held the NEW pane and every block recorded itself as its own origin. It is
   * advanced in an effect declared AFTER the placement effect below, so during
   * placement it still holds the previous value.
   */
  const cameFrom = useRef<string>("");

  // Place new panes, and remember what was focused when they appeared: that is
  // what the thread means. Done in an effect keyed on the pane list so it runs
  // once per genuinely new pane rather than on every render.
  useEffect(() => {
    const fresh = panes.filter((p) => !seen.current.has(p.id));
    if (fresh.length === 0) return;
    const from = cameFrom.current;
    setRects((prev) => {
      const next = { ...prev };
      let i = Object.keys(prev).length;
      for (const p of fresh) {
        next[p.id] = autoPlace(i);
        i += 1;
      }
      return next;
    });
    setOrigin((prev) => {
      const next = { ...prev };
      for (const p of fresh) {
        if (from && from !== p.id && seen.current.has(from)) next[p.id] = from;
      }
      return next;
    });
    fresh.forEach((p) => seen.current.add(p.id));
  }, [panes]);

  // Declared after the placement effect on purpose — see `cameFrom`.
  useEffect(() => {
    cameFrom.current = focusedPaneId;
  }, [focusedPaneId, panes]);

  const drag = useRef<{ id: string; dx: number; dy: number } | null>(null);
  /** Resize is the same gesture as drag with the opposite arithmetic. */
  const resize = useRef<{ id: string; x0: number; y0: number; w0: number; h0: number } | null>(
    null,
  );

  const onResizeDown = useCallback(
    (id: string) => (e: React.PointerEvent) => {
      const r = rects[id];
      if (!r) return;
      setFocusedPaneId(id);
      resize.current = { id, x0: e.clientX, y0: e.clientY, w0: r.w, h0: r.h };
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      e.stopPropagation();
      e.preventDefault();
    },
    [rects, setFocusedPaneId],
  );

  const onHeaderDown = useCallback(
    (pane: Pane) => (e: React.PointerEvent) => {
      // Buttons in the header must stay clickable.
      if ((e.target as HTMLElement).closest("button")) return;
      const r = rects[pane.id];
      if (!r) return;
      setFocusedPaneId(pane.id);
      drag.current = { id: pane.id, dx: e.clientX - r.x, dy: e.clientY - r.y };
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
      e.preventDefault();
    },
    [rects, setFocusedPaneId],
  );

  const onMove = useCallback((e: React.PointerEvent) => {
    const z = resize.current;
    if (z) {
      setRects((prev) => {
        const r = prev[z.id];
        if (!r) return prev;
        return {
          ...prev,
          [z.id]: {
            ...r,
            // Floors, not clamps to a grid: a block holding a long document
            // and a block holding a two-line claim want very different sizes,
            // and the person reading them knows which is which.
            w: Math.max(260, z.w0 + (e.clientX - z.x0)),
            h: Math.max(160, z.h0 + (e.clientY - z.y0)),
          },
        };
      });
      return;
    }
    const d = drag.current;
    if (!d) return;
    setRects((prev) => {
      const r = prev[d.id];
      if (!r) return prev;
      return {
        ...prev,
        [d.id]: { ...r, x: Math.max(0, e.clientX - d.dx), y: Math.max(0, e.clientY - d.dy) },
      };
    });
  }, []);

  const onUp = useCallback(() => {
    drag.current = null;
    resize.current = null;
  }, []);

  const threads = useMemo(
    () =>
      Object.entries(origin)
        .map(([to, from]) => {
          const a = rects[from];
          const b = rects[to];
          return a && b ? { key: `${from}->${to}`, d: thread(a, b) } : null;
        })
        .filter((t): t is { key: string; d: string } => t !== null),
    [origin, rects],
  );

  return (
    <div
      className="relative min-h-0 flex-1 overflow-auto"
      onPointerMove={onMove}
      onPointerUp={onUp}
      onPointerCancel={onUp}
      data-testid="board"
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          backgroundImage: "radial-gradient(var(--grid-dot) 1px, transparent 1px)",
          backgroundSize: "24px 24px",
        }}
      />

      {/* Provenance, drawn. Below the blocks so a thread never covers content. */}
      <svg aria-hidden className="pointer-events-none absolute inset-0 h-full w-full">
        {threads.map((t) => (
          <path
            key={t.key}
            d={t.d}
            fill="none"
            stroke="var(--accent)"
            strokeWidth="1"
            strokeDasharray="2 4"
            opacity="0.5"
          />
        ))}
      </svg>

      {panes.map((pane) => {
        const r = rects[pane.id];
        if (!r) return null;
        const surface = surfaces.get(pane.id);
        const lifecycle: BlockLifecycle = error
          ? "failed"
          : surface
            ? "settled"
            : connected
              ? "building"
              : "building";
        return (
          <div
            key={pane.id}
            className="absolute"
            style={{ left: r.x, top: r.y, width: r.w, height: r.h }}
            onPointerDown={() => setFocusedPaneId(pane.id)}
          >
            <Block
              title={pane.title || pane.kind}
              band="declarative"
              trust="signed"
              lifecycle={lifecycle}
              selected={pane.id === focusedPaneId}
              onClose={panes.length > 1 ? () => closePane(pane.id) : undefined}
              onHeaderPointerDown={onHeaderDown(pane)}
              onKeep={() => undefined}
              onAgain={() => undefined}
              onSources={() => undefined}
            >
              {surface ? (
                // The card impls call `useSurface()` for project scope and to
                // POST actions, so the tree must sit inside a SurfaceProvider.
                // Rendering <A2uiSurface> bare throws at mount.
                <SurfaceProvider projectId={projectId} surface={`${pane.kind}Surface`}>
                  <div className="p-2">
                    <A2uiSurface surface={surface} />
                  </div>
                </SurfaceProvider>
              ) : (
                <p className="p-3 font-mono text-[10.5px] text-ink-muted">
                  {error ?? (connected ? "waiting for the first frame…" : "connecting…")}
                </p>
              )}
            </Block>
            <span
              role="separator"
              aria-label={`Resize ${pane.title || pane.kind}`}
              onPointerDown={onResizeDown(pane.id)}
              className="absolute bottom-0 right-0 h-3.5 w-3.5 cursor-se-resize"
              style={{
                background:
                  "linear-gradient(135deg, transparent 0 55%, var(--border-strong) 55% 70%, transparent 70% 80%, var(--border-strong) 80% 95%, transparent 95%)",
              }}
            />
          </div>
        );
      })}

      {panes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center">
          <p className="font-mono text-[11px] text-ink-muted">
            open something from the rail — it lands here
          </p>
        </div>
      )}
    </div>
  );
}

export function Board({ projectId }: { projectId: string }) {
  const { panes } = useWorkspaceUI();
  return (
    <SurfaceStreamProvider projectId={projectId} panes={panes.map((p) => p.id)}>
      <BoardCanvas projectId={projectId} />
    </SurfaceStreamProvider>
  );
}

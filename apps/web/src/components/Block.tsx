/**
 * A Block — the one object the workbench is made of.
 *
 * A chat reply, a catalog-assembled surface, agent-written HTML and a
 * third-party panel are the SAME object at different sizes. Same frame, same
 * header, same four controls; only the contents differ. That is what removes
 * the two-paths problem at the design level rather than patching it in code —
 * previously "a card in chat" and "a surface in a pane" were different objects
 * with different renderers, and they drifted.
 *
 * Four things are always visible, because in a generative interface they are
 * primary rather than metadata:
 *
 *   TRUST      as WEIGHT, not colour — solid, half, dashed, dotted. Readable
 *              in greyscale, when printed, and by anyone who does not
 *              distinguish red from green. Hue is reserved for state.
 *   BAND       which of the four ways drew this: you built it, the agent
 *              assembled it from your parts, the agent wrote markup, or a third
 *              party shipped it.
 *   LIFECYCLE  streaming, settled, stale or failed. Generated content arrives
 *              over time; a frame that cannot say "still arriving" makes a
 *              half-built thing look finished.
 *   THE VERBS  keep · again, differently · sources. Generated things are
 *              ephemeral by default, so keeping is a deliberate act; and
 *              anything the agent made, it can make again another way.
 */
import type { ReactNode } from "react";

export type BlockTrust = "signed" | "earned" | "asserted" | "unverified";
export type BlockBand = "controlled" | "declarative" | "open" | "third-party";
export type BlockLifecycle = "building" | "settled" | "stale" | "failed";

/** Trust as a repeating pattern down the left edge. No hue involved. */
const TRUST_EDGE: Record<BlockTrust, string> = {
  signed: "var(--text-primary)",
  earned:
    "linear-gradient(180deg, var(--accent) 0 55%, var(--border-strong) 55% 100%)",
  asserted:
    "repeating-linear-gradient(180deg, var(--text-secondary) 0 7px, transparent 7px 13px)",
  unverified:
    "repeating-linear-gradient(180deg, var(--text-muted) 0 2px, transparent 2px 7px)",
};

const TRUST_LABEL: Record<BlockTrust, string> = {
  signed: "signed",
  earned: "earned",
  asserted: "agent",
  unverified: "unverified",
};

const BAND_LABEL: Record<BlockBand, string> = {
  controlled: "CTRL",
  declarative: "DECL",
  open: "OPEN",
  "third-party": "3RD",
};

/** How many of four bars are lit — the same information as the edge, at a glance. */
const TRUST_BARS: Record<BlockTrust, number> = {
  signed: 4,
  earned: 2,
  asserted: 1,
  unverified: 0,
};

export interface BlockProps {
  title: string;
  band: BlockBand;
  trust: BlockTrust;
  lifecycle?: BlockLifecycle;
  /** How many sources back this. Omitted when the block is not evidence-bearing. */
  sources?: number;
  selected?: boolean;
  onKeep?: () => void;
  onAgain?: () => void;
  onSources?: () => void;
  onClose?: () => void;
  /** Drag handle — the Board supplies this; the Block itself owns no layout. */
  onHeaderPointerDown?: (e: React.PointerEvent) => void;
  children: ReactNode;
}

function Lifecycle({ state }: { state: BlockLifecycle }) {
  if (state === "settled") {
    return <span className="font-mono text-[9.5px] text-ink-muted">settled</span>;
  }
  if (state === "building") {
    return (
      <span className="flex items-center gap-1.5">
        <span className="h-[5px] w-[5px] animate-pulse bg-accent" />
        <span className="font-mono text-[9.5px] text-ink-soft">building</span>
      </span>
    );
  }
  if (state === "stale") {
    return (
      <span className="font-mono text-[9.5px]" style={{ color: "var(--state-bad)" }}>
        stale
      </span>
    );
  }
  return (
    <span className="font-mono text-[9.5px]" style={{ color: "var(--state-bad)" }}>
      failed
    </span>
  );
}

export function Block({
  title,
  band,
  trust,
  lifecycle = "settled",
  sources,
  selected = false,
  onKeep,
  onAgain,
  onSources,
  onClose,
  onHeaderPointerDown,
  children,
}: BlockProps) {
  const lit = TRUST_BARS[trust];
  return (
    <div
      className="flex h-full min-h-0 bg-surface"
      style={{
        border: `1px solid ${selected ? "var(--accent)" : "var(--border-muted)"}`,
        boxShadow: selected ? "0 0 0 3px var(--accent-muted)" : "var(--shadow-sm)",
      }}
      data-testid="block"
      data-trust={trust}
      data-band={band}
    >
      <span
        aria-hidden
        className="w-[3px] shrink-0"
        style={{ background: TRUST_EDGE[trust] }}
        title={`trust: ${TRUST_LABEL[trust]}`}
      />
      <div className="flex min-w-0 flex-1 flex-col">
        <header
          onPointerDown={onHeaderPointerDown}
          className="flex shrink-0 items-center gap-2 border-b border-line px-2.5 py-1.5"
          style={{ cursor: onHeaderPointerDown ? "grab" : undefined }}
        >
          <span className="truncate font-mono text-[11px] font-medium text-ink">{title}</span>
          <span
            className="shrink-0 px-1.5 py-px font-mono text-[8.5px] tracking-[0.08em]"
            style={{ background: "var(--accent)", color: "var(--accent-fg)" }}
          >
            {BAND_LABEL[band]}
          </span>
          <span className="flex-1" />
          <Lifecycle state={lifecycle} />
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="shrink-0 px-1 font-mono text-[11px] leading-none text-ink-muted hover:text-ink"
              aria-label={`Close ${title}`}
            >
              ×
            </button>
          )}
        </header>

        <div className="min-h-0 flex-1 overflow-auto">{children}</div>

        <footer className="flex shrink-0 items-center gap-1.5 border-t border-line bg-sunken px-2.5 py-1.5">
          {onKeep && <Verb onClick={onKeep}>Keep</Verb>}
          {onAgain && <Verb onClick={onAgain}>Again</Verb>}
          {onSources && (
            <Verb onClick={onSources}>
              Sources{sources === undefined ? "" : ` · ${sources}`}
            </Verb>
          )}
          <span className="flex-1" />
          <span
            className="flex items-end gap-[2px]"
            title={`trust: ${TRUST_LABEL[trust]}`}
            aria-label={`trust: ${TRUST_LABEL[trust]}`}
          >
            {[0, 1, 2, 3].map((i) => (
              <span
                key={i}
                className="h-[10px] w-[2.5px]"
                style={{
                  background: i < lit ? "var(--accent)" : "var(--border-strong)",
                }}
              />
            ))}
          </span>
        </footer>
      </div>
    </div>
  );
}

function Verb({ onClick, children }: { onClick: () => void; children: ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="border border-line-strong px-2 py-[3px] font-mono text-[9.5px] text-ink hover:border-accent hover:text-accent"
    >
      {children}
    </button>
  );
}

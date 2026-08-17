import { renderChildCard } from "../surface-context";

import type { RendererProps } from "./_shared";

export function BriefsSurface({ component, onAction }: RendererProps) {
  const p = component.props as { badge_count?: number };
  const children = component.children ?? [];
  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs uppercase tracking-wider text-ink-muted">
          Briefs
        </div>
        {(p.badge_count ?? 0) > 0 && (
          <span className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-900">
            {p.badge_count}
          </span>
        )}
      </div>
      {children.length === 0 ? (
        <p className="text-sm text-ink-muted">No pending briefs.</p>
      ) : (
        <div className="flex-1 space-y-2 overflow-y-auto">
          {children.map((c) => {
            const pinnedCardId = c.id.startsWith("pinned-")
              ? c.id.slice("pinned-".length)
              : null;
            return (
              <div key={c.id} className="group relative">
                {pinnedCardId && (
                  <button
                    type="button"
                    onClick={() => onAction("unpin", { card_id: pinnedCardId })}
                    title="Unpin from Briefs"
                    aria-label="Unpin from Briefs"
                    className="absolute right-2 top-2 z-10 hidden rounded px-1.5 py-0.5 text-xs text-ink-muted hover:bg-sunken hover:text-ink group-hover:block"
                    data-testid={`unpin-${pinnedCardId}`}
                  >
                    ✕ Unpin
                  </button>
                )}
                {renderChildCard(c, onAction)}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

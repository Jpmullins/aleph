import { renderA2UI } from "../register";

import type { RendererProps } from "./_shared";

export function BriefsSurface({ component, onAction }: RendererProps) {
  const p = component.props as { badge_count?: number };
  const children = component.children ?? [];
  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 flex items-center justify-between">
        <div className="text-xs uppercase tracking-wider text-slate-400">
          Briefs
        </div>
        {(p.badge_count ?? 0) > 0 && (
          <span className="inline-flex items-center rounded-full bg-amber-100 px-2 py-0.5 text-[10px] font-semibold text-amber-900">
            {p.badge_count}
          </span>
        )}
      </div>
      {children.length === 0 ? (
        <p className="text-sm text-slate-500">No pending briefs.</p>
      ) : (
        <div className="flex-1 space-y-2 overflow-y-auto">
          {children.map((c) => (
            <div key={c.id}>{renderA2UI(c, onAction)}</div>
          ))}
        </div>
      )}
    </div>
  );
}

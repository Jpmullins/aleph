import { renderA2UI } from "../register";

import type { RendererProps } from "./_shared";

export function HypothesesSurface({ component, onAction }: RendererProps) {
  const children = component.children ?? [];
  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 text-xs uppercase tracking-wider text-slate-400">
        Hypotheses
      </div>
      {children.length === 0 ? (
        <p className="text-sm text-slate-500">
          No hypotheses yet — Hypothesis model lands in Increment 5.
        </p>
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

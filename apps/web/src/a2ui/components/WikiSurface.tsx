import { renderA2UI } from "../register";

import type { RendererProps } from "./_shared";

export function WikiSurface({ component, onAction }: RendererProps) {
  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 text-xs uppercase tracking-wider text-slate-400">
        WikiSurface
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto">
        {(component.children ?? []).map((c) => (
          <div key={c.id}>{renderA2UI(c, onAction)}</div>
        ))}
      </div>
    </div>
  );
}

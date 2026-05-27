import { renderA2UI } from "../register";

import type { RendererProps } from "./_shared";

export function ArtifactsSurface({ component, onAction }: RendererProps) {
  const children = component.children ?? [];
  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 text-xs uppercase tracking-wider text-slate-400">
        Artifacts
      </div>
      {children.length === 0 ? (
        <p className="text-sm text-slate-500">
          No artifacts yet — Builder lands in Increment 7.
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

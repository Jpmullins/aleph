import { renderA2UI } from "../register";

import type { RendererProps } from "./_shared";

export function NotesSurface({ component, onAction }: RendererProps) {
  const children = component.children ?? [];
  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 text-xs uppercase tracking-wider text-slate-400">
        Notes
      </div>
      {children.length === 0 ? (
        <p className="text-sm text-slate-500">
          No notes yet. POST a note via{" "}
          <code>/v1/projects/{"{id}"}/notes</code>.
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

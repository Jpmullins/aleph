import { useState } from "react";

import { NoteEditorCard } from "./NoteEditorCard";
import { SurfaceHeader, type RendererProps } from "./_shared";

interface NoteItem {
  id: string;
  title: string;
  body_md: string;
  section_id: string | null;
  updated_at: string | null;
}

/**
 * WP-4: the Notes tab renders ONLY from the surface data model (`{notes}`)
 * streamed by the backend — no `useQuery`, no polling. Create + body edits go
 * through `onAction` → the ledger-audited action router; the edit's result
 * returns as an `updateDataModel` delta.
 */
export function NotesSurface({ component, onAction }: RendererProps) {
  const notes = (component.props.notes as NoteItem[] | undefined) ?? [];
  const [selected, setSelected] = useState<string | null>(null);

  const selectedNote = notes.find((n) => n.id === selected) ?? null;

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Notes"
        subtitle={`${notes.length} notes`}
        actions={
          <button
            type="button"
            onClick={() => onAction("create_note", {})}
            className="rounded-md bg-[var(--accent,#0f172a)] px-3 py-1 text-xs font-medium text-white hover:opacity-90"
            data-testid="new-note"
          >
            + New
          </button>
        }
      />
      <div className="flex min-h-0 flex-1">
        <ul className="w-40 shrink-0 overflow-y-auto border-r border-[var(--border-muted,#e2e8f0)] p-2">
          {notes.length === 0 && (
            <li className="p-2 text-xs text-[var(--text-muted,#94a3b8)]">No notes. Click + New.</li>
          )}
          {notes.map((n) => (
            <li key={n.id}>
              <button
                type="button"
                onClick={() => setSelected(n.id)}
                className={
                  "w-full truncate rounded px-2 py-1.5 text-left text-xs " +
                  (n.id === selected
                    ? "bg-[var(--accent-muted,rgba(249,115,22,0.1))] text-[var(--accent,#0f172a)]"
                    : "text-[var(--text-secondary,#475569)] hover:bg-[var(--surface-sunken,#f8fafc)]")
                }
                data-testid={`note-${n.id}`}
              >
                {n.title}
              </button>
            </li>
          ))}
        </ul>
        <div className="min-w-0 flex-1">
          {selectedNote ? (
            <NoteEditorCard
              key={selectedNote.id}
              component={{
                type: "NoteEditorCard",
                id: `note-editor-${selectedNote.id}`,
                props: {
                  note_id: selectedNote.id,
                  section_id: selectedNote.section_id,
                  title: selectedNote.title,
                  body_md: selectedNote.body_md,
                },
              }}
              onAction={onAction}
            />
          ) : (
            <div className="p-6 text-sm text-[var(--text-muted,#94a3b8)]">
              Select a note, or click <strong>+ New</strong> to start writing.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

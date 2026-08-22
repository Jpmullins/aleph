import { useEffect, useRef, useState } from "react";

import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import type { RendererProps } from "./_shared";

/**
 * WP-4b — bound-props markdown note editor (a first-class catalog card).
 *
 * The sole in-place note editor (the legacy notebook-cell textarea path and the
 * inline `NotesSurface` editor were removed). Data arrives as bound props
 * (`note_id`, `section_id`,
 * `title`, `body_md`); every mutation is a ledger-audited `onAction`:
 *   - body edits → debounced `edit_note {section_id, body_md}`;
 *   - title rename → `rename_note {note_id, title}` (on blur / Enter);
 *   - "Promote to wiki" → `promote_note {note_id}`.
 * Markdown only. No fetch.
 */
interface NoteEditorProps {
  note_id?: string;
  section_id?: string | null;
  title?: string;
  body_md?: string;
}

export function NoteEditorCard({ component, onAction }: RendererProps) {
  const p = component.props as NoteEditorProps;
  const noteId = p.note_id ?? "";
  const sectionId = p.section_id ?? null;

  // Local buffers, re-seeded only when a DIFFERENT note loads (keyed on
  // note_id) so an inbound delta — including the echo of our own edit — never
  // clobbers the caret mid-type.
  const [body, setBody] = useState(p.body_md ?? "");
  const [title, setTitle] = useState(p.title ?? "");
  const [preview, setPreview] = useState(false);
  const [saved, setSaved] = useState<"idle" | "saving" | "saved" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setBody(p.body_md ?? "");
    setTitle(p.title ?? "");
    setSaved("idle");
  }, [noteId]);

  /**
   * Autosave, and it has to be honest about two things it was not.
   *
   * 1. **A note with no section discarded every edit.** `_notes_messages` binds
   *    `section_id: null` for a note that has no sections — a promoted or
   *    imported one — and the write was guarded on that value, so typing into
   *    such a note dispatched nothing at all. The server creates the section on
   *    first write now; the card just has to send the note id with the body.
   * 2. **The badge said "Saved" before the request had left the browser.**
   *    `setSaved("saved")` sat on the line after the dispatch. An autosave that
   *    500s reported success, which is worse than reporting nothing.
   */
  const save = async (text: string) => {
    const target = sectionId
      ? { section_id: sectionId, body_md: text }
      : noteId
        ? { note_id: noteId, body_md: text }
        : null;
    if (!target) {
      // No note and no section: there is nothing on the server to write to, and
      // saying so is the only honest badge.
      setSaved("failed");
      return;
    }
    const ok = await onAction("edit_note", target);
    setSaved(ok === false ? "failed" : "saved");
  };

  const onChangeBody = (text: string) => {
    setBody(text);
    setSaved("saving");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void save(text), 700);
  };

  const commitTitle = () => {
    const next = title.trim();
    if (noteId && next && next !== (p.title ?? "")) {
      onAction("rename_note", { note_id: noteId, title: next });
    }
  };

  return (
    <div className="flex h-full flex-col p-3" data-testid="note-editor-card">
      <div className="mb-2 flex items-center gap-2">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onBlur={commitTitle}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              (e.target as HTMLInputElement).blur();
            }
          }}
          placeholder="Untitled note"
          className="min-w-0 flex-1 truncate border border-transparent bg-transparent px-1 py-0.5 text-sm font-semibold text-ink hover:border-line focus:border-accent focus:outline-none"
          data-testid="note-title"
        />
        <span
          className={saved === "failed" ? "text-xs text-badge-failed-fg" : "text-xs text-ink-muted"}
          data-testid="note-save-state"
        >
          {saved === "saving"
            ? "Saving…"
            : saved === "saved"
              ? "Saved"
              : saved === "failed"
                ? "Not saved"
                : ""}
        </span>
        <button
          type="button"
          onClick={() => setPreview((v) => !v)}
          className="border border-line px-2 py-1 text-xs text-ink-soft"
          data-testid="note-preview-toggle"
        >
          {preview ? "Edit" : "Preview"}
        </button>
        <button
          type="button"
          onClick={() => noteId && onAction("promote_note", { note_id: noteId })}
          className="bg-accent px-2 py-1 text-xs font-medium text-accent-fg hover:opacity-90"
          data-testid="note-promote"
        >
          Promote to wiki
        </button>
      </div>
      {preview ? (
        <div className="flex-1 overflow-y-auto border border-line p-3">
          <WikiBodyMarkdown body={body || "_Nothing to preview yet._"} />
        </div>
      ) : (
        <textarea
          value={body}
          onChange={(e) => onChangeBody(e.target.value)}
          placeholder="Write in markdown. Use [[Page Title]] to link the wiki. Autosaves."
          className="flex-1 resize-none border border-line bg-surface p-3 font-mono text-xs text-ink focus:border-accent focus:outline-none"
          data-testid="note-editor"
        />
      )}
    </div>
  );
}

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { useSurface } from "../register";
import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { api } from "@/lib/api";
import { SurfaceHeader, type RendererProps } from "./_shared";

interface NoteOut {
  id: string;
  title: string;
}
interface NoteSectionOut {
  id: string;
  note_id: string;
  ordinal: number;
  body_md: string;
  anchor: string | null;
}
interface NoteDetail {
  note: NoteOut;
  sections: NoteSectionOut[];
}

export function NotesSurface(_: RendererProps) {
  const { projectId } = useSurface();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);

  const notes = useQuery<NoteOut[]>({
    queryKey: ["notes", projectId],
    queryFn: () => api.get<NoteOut[]>(`/v1/projects/${projectId}/notes`),
  });

  const createNote = useMutation({
    mutationFn: async () =>
      api.post<NoteOut>(`/v1/projects/${projectId}/notes`, { title: "Untitled note" }),
    onSuccess: (n) => {
      qc.invalidateQueries({ queryKey: ["notes", projectId] });
      setSelected(n.id);
    },
  });

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Notes"
        subtitle={notes.data ? `${notes.data.length} notes` : undefined}
        actions={
          <button
            type="button"
            onClick={() => createNote.mutate()}
            className="rounded-md bg-[var(--accent,#0f172a)] px-3 py-1 text-xs font-medium text-white hover:opacity-90"
            data-testid="new-note"
          >
            + New
          </button>
        }
      />
      <div className="flex min-h-0 flex-1">
        <ul className="w-40 shrink-0 overflow-y-auto border-r border-[var(--border-muted,#e2e8f0)] p-2">
          {notes.data?.length === 0 && (
            <li className="p-2 text-xs text-[var(--text-muted,#94a3b8)]">
              No notes. Click + New.
            </li>
          )}
          {notes.data?.map((n) => (
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
          {selected ? (
            <NoteEditor projectId={projectId} noteId={selected} />
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

function NoteEditor({ projectId, noteId }: { projectId: string; noteId: string }) {
  const qc = useQueryClient();
  const [body, setBody] = useState("");
  const [preview, setPreview] = useState(false);
  const [saved, setSaved] = useState<"idle" | "saving" | "saved">("idle");
  const sectionId = useRef<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const detail = useQuery<NoteDetail>({
    queryKey: ["note", projectId, noteId],
    queryFn: () => api.get<NoteDetail>(`/v1/projects/${projectId}/notes/${noteId}`),
  });

  // Load the (first) section's body when the note loads.
  useEffect(() => {
    const s = detail.data?.sections[0];
    sectionId.current = s?.id ?? null;
    setBody(s?.body_md ?? "");
    setSaved("idle");
  }, [detail.data, noteId]);

  const save = async (text: string) => {
    setSaved("saving");
    if (sectionId.current) {
      await api.patch(
        `/v1/projects/${projectId}/notes/${noteId}/sections/${sectionId.current}`,
        { body_md: text },
      );
    } else {
      const s = await api.post<NoteSectionOut>(
        `/v1/projects/${projectId}/notes/${noteId}/sections`,
        { body_md: text },
      );
      sectionId.current = s.id;
      qc.invalidateQueries({ queryKey: ["note", projectId, noteId] });
    }
    setSaved("saved");
  };

  const onChange = (text: string) => {
    setBody(text);
    setSaved("saving");
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void save(text), 700);
  };

  const promote = useMutation({
    mutationFn: async () =>
      api.post(`/v1/projects/${projectId}/notes/${noteId}/promote`, {}),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["surface", projectId] }),
  });

  return (
    <div className="flex h-full flex-col p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="text-xs text-[var(--text-muted,#94a3b8)]">
          {saved === "saving" ? "Saving…" : saved === "saved" ? "Saved" : ""}
        </span>
        <button
          type="button"
          onClick={() => setPreview((p) => !p)}
          className="ml-auto rounded border border-[var(--border-muted,#e2e8f0)] px-2 py-1 text-xs text-[var(--text-secondary,#475569)]"
        >
          {preview ? "Edit" : "Preview"}
        </button>
        <button
          type="button"
          onClick={() => promote.mutate()}
          disabled={promote.isPending || !body.trim()}
          className="rounded bg-[var(--accent,#f97316)] px-2 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          data-testid="promote-note"
          title="Create a draft wiki page + approval proposal from this note"
        >
          {promote.isPending ? "Promoting…" : promote.isSuccess ? "✓ In Briefs" : "Promote to wiki"}
        </button>
      </div>
      {preview ? (
        <div className="flex-1 overflow-y-auto rounded-md border border-[var(--border-muted,#e2e8f0)] p-3">
          <WikiBodyMarkdown body={body || "_Nothing to preview yet._"} />
        </div>
      ) : (
        <textarea
          value={body}
          onChange={(e) => onChange(e.target.value)}
          placeholder="Write in markdown. Use [[Page Title]] to link the wiki. Autosaves; Promote to wiki turns this into a draft page for approval."
          className="flex-1 resize-none rounded-md border border-[var(--border-muted,#e2e8f0)] bg-[var(--surface-raised,#fff)] p-3 font-mono text-xs text-[var(--text-primary,#0f172a)] focus:border-[var(--accent,#f97316)] focus:outline-none"
          data-testid="note-editor"
        />
      )}
    </div>
  );
}

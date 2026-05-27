import { useEffect, useRef, useState } from "react";

import { CardShell, type RendererProps } from "./_shared";

export function NotebookCellCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    section_id: string;
    body_md: string;
    ordinal: number;
  };
  const [draft, setDraft] = useState(p.body_md);
  const timer = useRef<number | null>(null);
  useEffect(() => setDraft(p.body_md), [p.body_md]);
  const onChange = (value: string) => {
    setDraft(value);
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      onAction("edit_note", { section_id: p.section_id, body_md: value });
    }, 750);
  };
  return (
    <CardShell subtitle={<span>Section {p.ordinal}</span>}>
      <textarea
        value={draft}
        onChange={(e) => onChange(e.target.value)}
        rows={Math.max(3, draft.split("\n").length)}
        className="w-full resize-none rounded border border-slate-200 bg-white p-2 text-sm font-mono"
      />
    </CardShell>
  );
}

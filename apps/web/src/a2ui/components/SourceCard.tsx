import { useSurface } from "../register";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

export function SourceCard({ component, onAction }: RendererProps) {
  const { projectId, surface } = useSurface();
  const p = component.props as {
    source_id: string;
    short_id: string;
    title: string;
    url?: string | null;
    status: string;
  };
  const tone =
    p.status === "wiki_done"
      ? "emerald"
      : p.status.includes("failed")
        ? "red"
        : p.status === "indexed"
          ? "sky"
          : "slate";
  return (
    <CardShell
      title={`${p.short_id} · ${p.title}`}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={tone}>{p.status}</Pill>
          {p.url && (
            <a
              href={p.url}
              target="_blank"
              rel="noreferrer"
              className="truncate text-xs text-slate-500 hover:text-slate-900"
            >
              {p.url}
            </a>
          )}
        </span>
      }
      actions={
        <FeedbackButton
          projectId={projectId}
          targetKind="source"
          targetId={p.source_id}
          surface={surface}
        />
      }
    >
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => onAction("open", { target_id: p.source_id, target_kind: "source" })}
          className="text-xs text-slate-500 hover:text-slate-900"
        >
          Open source
        </button>
        <button
          type="button"
          onClick={() =>
            onAction("navigate_wiki", { page_id: p.source_id })
          }
          className="text-xs text-slate-500 hover:text-slate-900"
        >
          Open source page
        </button>
      </div>
    </CardShell>
  );
}

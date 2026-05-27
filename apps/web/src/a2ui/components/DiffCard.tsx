import { CardShell, type RendererProps } from "./_shared";

export function DiffCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    from_revision_id: string;
    to_revision_id: string;
    page_id: string;
  };
  return (
    <CardShell
      title="Wiki revision diff"
      subtitle={
        <span className="text-xs">
          {p.from_revision_id.slice(0, 8)} → {p.to_revision_id.slice(0, 8)}
        </span>
      }
    >
      <button
        type="button"
        onClick={() =>
          onAction("open", {
            target_id: p.page_id,
            target_kind: "wiki_revision_diff",
          })
        }
        className="text-xs text-slate-500 hover:text-slate-900"
      >
        Open side-by-side diff →
      </button>
    </CardShell>
  );
}

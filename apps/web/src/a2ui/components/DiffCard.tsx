import { CardShell, type RendererProps } from "./_shared";

export function DiffCard({ component }: RendererProps) {
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
      <p className="text-xs text-slate-500">
        Revisions {p.from_revision_id.slice(0, 8)} → {p.to_revision_id.slice(0, 8)}.
      </p>
    </CardShell>
  );
}

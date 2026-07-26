import { useSurface } from "../surface-context";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

export function FindingCard({ component, onAction }: RendererProps) {
  const { surface } = useSurface();
  const p = component.props as {
    finding_id: string;
    severity: "info" | "low" | "medium" | "high";
    kind: string;
    summary: string;
  };
  const tone = p.severity === "high" ? "red" : p.severity === "medium" ? "amber" : "slate";
  return (
    <CardShell
      title={p.kind}
      subtitle={<Pill tone={tone}>{p.severity}</Pill>}
      actions={
        <FeedbackButton
          onAction={onAction}
          targetKind="finding"
          targetId={p.finding_id}
          surface={surface}
        />
      }
    >
      <p className="text-sm text-ink-soft">{p.summary}</p>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          onClick={() =>
            onAction("open", { target_id: p.finding_id, target_kind: "review_finding" })
          }
          className="text-xs text-ink-muted hover:text-ink"
        >
          Open finding
        </button>
        <button
          type="button"
          onClick={() =>
            onAction("approve", {
              target_id: p.finding_id,
              target_kind: "review_finding",
            })
          }
          className="ml-auto text-xs text-emerald-700 hover:text-emerald-900"
        >
          Resolve
        </button>
        <button
          type="button"
          onClick={() =>
            onAction("reject", {
              target_id: p.finding_id,
              target_kind: "review_finding",
              reason: "dismissed",
            })
          }
          className="text-xs text-red-700 hover:text-red-900"
        >
          Dismiss
        </button>
      </div>
    </CardShell>
  );
}

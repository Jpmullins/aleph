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
  const tone = p.severity === "high" ? "bad" : p.severity === "medium" ? "warn" : "neutral";
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
          className="ml-auto text-xs text-good hover:opacity-80"
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
          className="text-xs text-bad hover:opacity-80"
        >
          Dismiss
        </button>
      </div>
    </CardShell>
  );
}

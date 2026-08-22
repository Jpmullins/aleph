import { useSurface } from "../surface-context";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

export function FindingCard({ component, onAction }: RendererProps) {
  const { surface } = useSurface();
  const p = component.props as {
    finding_id: string;
    severity: "info" | "low" | "medium" | "high";
    kind: string;
    summary: string;
    evidence_refs?: Array<{ kind: string; id: string; label?: string }>;
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
      {/* `evidence_refs` is what a reviewer needs to judge the finding, and it
          was reaching the browser already: `routes/surfaces.py` reads
          `ReviewFinding.evidence_refs_jsonb`, `finding_card()` sends it, the
          zod schema declares it, the binder resolved it — and this view did not
          destructure it, so every reviewer saw a summary with no evidence
          behind it. Same list, same shape, same markup as `ApprovalCard`. */}
      {p.evidence_refs && p.evidence_refs.length > 0 && (
        <ul
          className="mt-2 list-disc pl-5 text-xs text-ink-muted"
          data-testid="finding-evidence-refs"
        >
          {p.evidence_refs.map((e) => (
            <li key={e.id}>
              {e.kind}: {e.label ?? e.id}
            </li>
          ))}
        </ul>
      )}
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

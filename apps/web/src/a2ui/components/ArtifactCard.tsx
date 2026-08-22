import { useSurface } from "../surface-context";
import { CardShell, Pill, type RendererProps } from "./_shared";

const KIND_TONE: Record<string, "info" | "good" | "warn" | "neutral"> = {
  report_pdf: "info",
  report_docx: "info",
  report_markdown_bundle: "info",
  source_pack: "good",
  deck_pdf: "warn",
};

export function ArtifactCard({ component, onAction }: RendererProps) {
  const { surface } = useSurface();
  const p = component.props as {
    artifact_id: string;
    short_id?: string;
    title: string;
    artifact_kind: string;
    status: string;
    drifted?: boolean;
  };
  const terminal = p.status === "ready" || p.status === "done" || !!p.short_id;
  const statusTone =
    p.status.includes("fail") || p.status.includes("error")
      ? "bad"
      : terminal
        ? "good"
        : "warn";
  const titleLabel = p.short_id ? `${p.short_id} · ${p.title}` : p.title;
  return (
    <CardShell
      title={titleLabel}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={KIND_TONE[p.artifact_kind] ?? "neutral"}>
            {p.artifact_kind.replace(/_/g, " ")}
          </Pill>
          <Pill tone={statusTone}>{p.status}</Pill>
          {p.drifted && (
            <Pill tone="warn">
              <span data-testid={`artifact-drifted-${p.artifact_id}`}>drifted</span>
            </Pill>
          )}
        </span>
      }
    >
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() =>
            onAction("open", { target_id: p.artifact_id, target_kind: "artifact" })
          }
          className="text-xs font-medium text-accent hover:opacity-80"
          data-testid={`artifact-open-${p.artifact_id}`}
        >
          Open in Library
        </button>
        {!terminal && (
          <span className="ml-auto text-xs text-ink-muted" data-surface={surface}>
            building…
          </span>
        )}
      </div>
    </CardShell>
  );
}

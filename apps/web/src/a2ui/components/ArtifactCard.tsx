import { useSurface } from "../surface-context";
import { CardShell, Pill, type RendererProps } from "./_shared";

const KIND_TONE: Record<string, "sky" | "emerald" | "amber" | "slate"> = {
  report_pdf: "sky",
  report_docx: "sky",
  report_markdown_bundle: "sky",
  source_pack: "emerald",
  deck_pdf: "amber",
};

export function ArtifactCard({ component, onAction }: RendererProps) {
  const { surface } = useSurface();
  const p = component.props as {
    artifact_id: string;
    short_id?: string;
    title: string;
    artifact_kind: string;
    status: string;
  };
  const terminal = p.status === "ready" || p.status === "done" || !!p.short_id;
  const statusTone =
    p.status.includes("fail") || p.status.includes("error")
      ? "red"
      : terminal
        ? "emerald"
        : "amber";
  const titleLabel = p.short_id ? `${p.short_id} · ${p.title}` : p.title;
  return (
    <CardShell
      title={titleLabel}
      subtitle={
        <span className="flex items-center gap-2">
          <Pill tone={KIND_TONE[p.artifact_kind] ?? "slate"}>
            {p.artifact_kind.replace(/_/g, " ")}
          </Pill>
          <Pill tone={statusTone}>{p.status}</Pill>
        </span>
      }
    >
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() =>
            onAction("open", { target_id: p.artifact_id, target_kind: "artifact" })
          }
          className="text-xs font-medium text-[var(--accent,#f97316)] hover:opacity-80"
          data-testid={`artifact-open-${p.artifact_id}`}
        >
          Open in Library
        </button>
        {!terminal && (
          <span className="ml-auto text-xs text-slate-400" data-surface={surface}>
            building…
          </span>
        )}
      </div>
    </CardShell>
  );
}

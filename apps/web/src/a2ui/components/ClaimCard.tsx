import { CardShell, Pill, type RendererProps } from "./_shared";

export function ClaimCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    claim_id: string;
    text: string;
    confidence: string;
    citations?: Array<{ marker: string; source_short_id?: string | null }>;
  };
  const tone =
    p.confidence === "well-supported"
      ? "emerald"
      : p.confidence === "contested"
        ? "amber"
        : p.confidence === "uncited"
          ? "red"
          : "slate";
  return (
    <CardShell subtitle={<Pill tone={tone}>{p.confidence}</Pill>}>
      <p className="text-sm text-slate-700">{p.text}</p>
      {p.citations && p.citations.length > 0 && (
        <div className="mt-2 text-xs text-slate-500">
          {p.citations.map((c) => (
            <span
              key={c.marker}
              className="mr-1 inline-flex rounded bg-amber-50 px-1 py-0.5 text-amber-900"
            >
              {c.marker}
            </span>
          ))}
        </div>
      )}
      <button
        type="button"
        onClick={() => onAction("open", { target_id: p.claim_id, target_kind: "claim" })}
        className="mt-2 text-xs text-slate-500 hover:text-slate-900"
      >
        Open claim →
      </button>
    </CardShell>
  );
}

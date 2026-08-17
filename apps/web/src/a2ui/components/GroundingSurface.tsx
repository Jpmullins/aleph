import type { RendererProps } from "./_shared";

/**
 * What a claim actually rests on: claim → citation → chunk → character span.
 *
 * Every hop in this chain existed in the schema and carried nothing — the
 * citation's `source_page_id` was NULL at every production write site, and
 * `chunk_ids` was `[]`. An inspector built before those writers were fixed
 * would have rendered a confident, authoritative *empty* chain, which is worse
 * than having no inspector: it tells the analyst the claim was checked.
 *
 * So the states this component treats as first-class are the negative ones. An
 * ungrounded claim is not an error and is not hidden — it is the single most
 * important thing this surface can say, and it is said plainly, with the reason
 * the chain stopped where it did.
 *
 * Data-bound only: every value comes from `props`, pushed by the server as an
 * `updateDataModel` delta. No fetching here — a pane owns no transport, so what
 * is drawn is exactly what the server resolved.
 */

interface SourceInfo {
  id: string;
  short_id?: string | null;
  title?: string | null;
  url?: string | null;
  retracted?: boolean;
}

interface ChunkInfo {
  id: string;
  ordinal: number;
  text: string;
  char_start: number;
  char_end: number;
  section_path?: string | null;
}

interface Grounding {
  marker?: string | null;
  source?: SourceInfo | null;
  chunks?: ChunkInfo[];
}

interface ClaimInfo {
  id: string;
  text: string;
  confidence?: string | null;
  page_id?: string | null;
  page_title?: string | null;
}

const CONFIDENCE_STYLES: Record<string, string> = {
  cited: "bg-emerald-100 text-emerald-900",
  inferred: "bg-amber-100 text-amber-900",
  contested: "bg-rose-100 text-rose-900",
  retracted: "bg-rose-200 text-rose-950 line-through",
};

/** Why this citation reached no readable text — stated, never elided. */
function gapReason(g: Grounding): string | null {
  const chunks = g.chunks ?? [];
  if (chunks.length > 0) return null;
  if (!g.source) {
    return "This citation is not linked to a source document, so there is no text to show.";
  }
  return "This citation names a source but no specific passage within it.";
}

export function GroundingSurface({ component }: RendererProps) {
  const p = component.props as {
    claim?: ClaimInfo | null;
    groundings?: Grounding[];
  };
  const claim = p.claim ?? null;
  const groundings = p.groundings ?? [];

  if (!claim) {
    return (
      <div className="flex h-full flex-col p-3">
        <div className="mb-2 text-xs uppercase tracking-wider text-ink-muted">Grounding</div>
        <p className="text-sm text-ink-muted">
          Select a claim to see what it rests on.
        </p>
      </div>
    );
  }

  const totalChunks = groundings.reduce((n, g) => n + (g.chunks?.length ?? 0), 0);
  const grounded = totalChunks > 0;

  return (
    <div className="flex h-full flex-col p-3" data-testid="grounding-surface">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-xs uppercase tracking-wider text-ink-muted">Grounding</div>
        <span
          data-testid="grounding-status"
          className={
            "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold " +
            (grounded ? "bg-emerald-100 text-emerald-900" : "bg-amber-100 text-amber-900")
          }
        >
          {grounded
            ? `${totalChunks} passage${totalChunks === 1 ? "" : "s"}`
            : "ungrounded"}
        </span>
      </div>

      <div className="mb-3 rounded-md border border-line bg-elevated p-2.5">
        <p className="text-sm text-ink">{claim.text}</p>
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {claim.confidence && (
            <span
              className={
                "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium " +
                (CONFIDENCE_STYLES[claim.confidence] ?? "bg-sunken text-ink-soft")
              }
            >
              {claim.confidence}
            </span>
          )}
          {claim.page_title && (
            <span className="text-[11px] text-ink-muted">on {claim.page_title}</span>
          )}
        </div>
      </div>

      {groundings.length === 0 ? (
        <p className="text-sm text-amber-700" data-testid="grounding-empty">
          This claim has no citations at all. Nothing in the corpus is recorded as
          supporting it.
        </p>
      ) : (
        <ol className="flex-1 space-y-3 overflow-y-auto">
          {groundings.map((g, i) => {
            const chunks = g.chunks ?? [];
            const gap = gapReason(g);
            return (
              <li
                key={`${g.marker ?? "cite"}-${i}`}
                className="rounded-md border border-line p-2.5"
                data-testid="grounding-citation"
              >
                <div className="mb-1.5 flex flex-wrap items-baseline gap-1.5">
                  {g.marker && (
                    <span className="font-mono text-[11px] text-ink-soft">{g.marker}</span>
                  )}
                  {g.source ? (
                    <>
                      <span className="text-xs font-medium text-ink">
                        {g.source.title || g.source.short_id || g.source.id}
                      </span>
                      {g.source.retracted && (
                        <span className="inline-flex items-center rounded bg-rose-200 px-1.5 py-0.5 text-[10px] font-semibold text-rose-950">
                          retracted
                        </span>
                      )}
                    </>
                  ) : (
                    <span className="text-xs text-ink-muted">unlinked source</span>
                  )}
                </div>

                {gap ? (
                  <p className="text-xs text-amber-700" data-testid="grounding-gap">
                    {gap}
                  </p>
                ) : (
                  <ul className="space-y-1.5">
                    {chunks.map((c) => (
                      <li key={c.id} className="rounded bg-sunken p-2">
                        <blockquote className="whitespace-pre-wrap text-xs text-ink-soft">
                          {c.text}
                        </blockquote>
                        <div className="mt-1 flex flex-wrap gap-2 text-[10px] text-ink-muted">
                          {c.section_path && <span>{c.section_path}</span>}
                          {/* The offsets are the point: they resolve this quote
                              to an exact span a reader can go and check. */}
                          <span className="font-mono">
                            chars {c.char_start}–{c.char_end}
                          </span>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

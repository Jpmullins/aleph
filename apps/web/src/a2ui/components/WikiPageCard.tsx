import { useMemo, useState } from "react";

import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { useWorkspaceUI } from "@/lib/workspace-ui";
import { HtmlDocCard } from "./HtmlDocCard";
import { CardShell, FeedbackButton, Pill, type RendererProps } from "./_shared";

/**
 * WP-4b — the rich wiki reader, a first-class catalog card.
 *
 * Receives its data ENTIRELY as bound props (no fetch): `body_md`, `claims`,
 * `citations`, `wikilinks_out`, `page_meta`, `html_url`. Reuses the existing
 * `WikiBodyMarkdown` tokenizer, upgraded so:
 *   - `[[wikilink]]` clicks emit an `navigate_wiki` A2UI action (ledger-audited
 *     router), not client-only navigation;
 *   - `[cN]` citation markers open a popover resolving to the source title+url
 *     from the bound `citations` list;
 *   - claims render confidence badges (from `WikiClaim.confidence`);
 *   - a freshness badge slot renders `page_meta.freshness` if present, else "—"
 *     (WP-6 populates it — no coupling here).
 *   - approve / reject / repair-links ride the ledger-audited action router.
 *
 * Agent-composed dossier/ACH pages set `derived`+`read_only` to hide the
 * curation actions. No `dangerouslySetInnerHTML`.
 */
interface Claim {
  id: string;
  text: string;
  confidence: string;
  section_anchor: string | null;
}

interface Citation {
  marker: string;
  source_title: string | null;
  url: string | null;
  source_page_id: string | null;
}

interface Wikilink {
  dst_title: string;
  dst_page_id: string | null;
  occurrences: number;
}

interface PageMeta {
  page_id?: string;
  title?: string;
  status?: string;
  is_stub?: boolean;
  freshness?: string | null;
}

const CONFIDENCE_TONE: Record<string, "emerald" | "amber" | "red" | "sky" | "slate"> = {
  "well-supported": "emerald",
  well_supported: "emerald",
  cited: "sky",
  "weakly-supported": "amber",
  weakly_supported: "amber",
  contested: "amber",
  uncited: "red",
  retracted: "red",
};

const STATUS_TONE: Record<string, "emerald" | "amber" | "slate"> = {
  approved: "emerald",
  draft: "amber",
  archived: "slate",
};

function CitationBadge({ marker, citation }: { marker: string; citation: Citation | undefined }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative mx-0.5 inline-flex">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title={citation?.source_title ?? marker}
        className="inline-flex items-baseline rounded bg-amber-100 px-1 text-[10px] font-semibold uppercase tracking-wider text-amber-900 hover:bg-amber-200"
        data-testid={`citation-${marker}`}
      >
        {marker}
      </button>
      {open && (
        <span
          className="absolute left-0 top-5 z-20 w-64 rounded-md border border-line-strong bg-surface p-2 text-xs shadow-lg"
          data-testid={`citation-popover-${marker}`}
        >
          {citation ? (
            <>
              <span className="block font-medium text-ink">
                {citation.source_title ?? "Unknown source"}
              </span>
              {citation.url && (
                <a
                  href={citation.url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-1 block break-words text-sky-700 underline"
                >
                  {citation.url}
                </a>
              )}
            </>
          ) : (
            <span className="text-ink-muted">No citation resolved for {marker}.</span>
          )}
        </span>
      )}
    </span>
  );
}

export function WikiPageCard({ component, onAction }: RendererProps) {
  const p = component.props as {
    body_md?: string;
    claims?: Claim[];
    citations?: Citation[];
    wikilinks_out?: Wikilink[];
    page_meta?: PageMeta;
    html_url?: string | null;
    retracted?: boolean;
    derived?: boolean;
    read_only?: boolean;
  };
  const bodyMd = p.body_md ?? "";
  const claims = p.claims ?? [];
  const citations = p.citations ?? [];
  const wikilinks = p.wikilinks_out ?? [];
  const meta = p.page_meta ?? {};
  const readOnly = Boolean(p.read_only || p.derived);
  const [view, setView] = useState<"reader" | "document">("reader");
  const [rejecting, setRejecting] = useState(false);
  const [rejectReason, setRejectReason] = useState("");

  // WP-4d: publish the analyst's claim/text selection into shared workspace
  // state so the agent sees it, and read the agent-requested highlight.
  const { setSelection, highlightedClaimId } = useWorkspaceUI();
  const pageId = meta.page_id ?? null;
  const selectClaim = (claim: Claim) =>
    setSelection({ claim_id: claim.id, text: claim.text, page_id: pageId });
  const publishTextSelection = () => {
    const text =
      typeof window !== "undefined" ? (window.getSelection()?.toString().trim() ?? "") : "";
    if (text) setSelection({ claim_id: null, text, page_id: pageId });
  };

  const linkMap = useMemo(() => {
    const map = new Map<string, string | null>();
    for (const l of wikilinks) map.set(l.dst_title, l.dst_page_id);
    return map;
  }, [wikilinks]);

  const citationMap = useMemo(() => {
    const map = new Map<string, Citation>();
    for (const c of citations) map.set(c.marker, c);
    return map;
  }, [citations]);

  const brokenLinks = wikilinks.filter((l) => l.dst_page_id === null).length;

  const navigateByTitle = (title: string) => {
    const dst = linkMap.get(title);
    if (dst) onAction("navigate_wiki", { page_id: dst });
  };
  const resolveLink = (title: string): boolean | null =>
    linkMap.has(title) ? linkMap.get(title) !== null : null;
  const openPageId = (id: string) => onAction("navigate_wiki", { page_id: id });

  const status = meta.status ?? "";
  const canCurate = !readOnly && status !== "archived";

  return (
    <div className="p-4" data-testid="wiki-page-card" onMouseUp={publishTextSelection}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {meta.title && (
          <span className="text-sm font-semibold text-ink">{meta.title}</span>
        )}
        {status && (
          <Pill tone={STATUS_TONE[status] ?? "slate"}>
            <span data-testid="wiki-status-badge">{status}</span>
          </Pill>
        )}
        {meta.is_stub && <Pill tone="amber">stub</Pill>}
        {p.retracted && (
          <Pill tone="red">
            <span data-testid="wiki-retracted-badge">⚠ retracted source</span>
          </Pill>
        )}
        <Pill tone="slate">
          <span data-testid="wiki-freshness">freshness: {meta.freshness ?? "—"}</span>
        </Pill>
        {p.html_url && (
          <button
            type="button"
            onClick={() => setView((v) => (v === "reader" ? "document" : "reader"))}
            className="rounded border border-line-strong px-2 py-0.5 text-xs text-ink-soft hover:bg-elevated"
            data-testid="wiki-view-toggle"
          >
            {view === "reader" ? "Document view" : "Reader view"}
          </button>
        )}
        {meta.page_id && (
          <span className="ml-auto">
            <FeedbackButton
              targetKind="wiki_page"
              targetId={meta.page_id}
              surface="WikiSurface"
              onAction={onAction}
            />
          </span>
        )}
      </div>

      {canCurate && meta.page_id && status !== "approved" && (
        <div className="mb-3">
          {!rejecting ? (
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={() =>
                  onAction("approve", { target_id: meta.page_id, target_kind: "wiki_page" })
                }
                className="rounded bg-emerald-600 px-3 py-1 text-xs font-medium text-ink-inverse hover:bg-emerald-700"
                data-testid="wiki-approve"
              >
                Approve
              </button>
              <button
                type="button"
                onClick={() => setRejecting(true)}
                className="rounded border border-line-strong px-3 py-1 text-xs font-medium text-ink-soft hover:bg-elevated"
                data-testid="wiki-reject"
              >
                Reject
              </button>
            </div>
          ) : (
            // The reason is not a formality: the wiki agent reads it back on
            // the next compile so it does not reproduce the rejected content.
            // Rejecting with an empty reason discards the only signal the
            // analyst has, so the confirm button stays disabled until there is
            // one and the server rejects an empty reason outright
            // (`RejectPageIn.reason` is `min_length=1`).
            <div className="rounded-md border border-line bg-sunken p-2">
              <label
                htmlFor="wiki-reject-reason"
                className="mb-1 block text-xs font-medium text-ink-soft"
              >
                Why is this page wrong? The agent reads this before rewriting it.
              </label>
              <textarea
                id="wiki-reject-reason"
                autoFocus
                rows={2}
                value={rejectReason}
                onChange={(e) => setRejectReason(e.target.value)}
                placeholder="e.g. conflates two different trials; the 2019 figure is from the retracted paper"
                className="w-full rounded border border-line px-2 py-1 text-xs focus:border-line-strong focus:outline-none"
                data-testid="wiki-reject-reason"
              />
              <div className="mt-2 flex items-center gap-2">
                <button
                  type="button"
                  disabled={!rejectReason.trim()}
                  onClick={() => {
                    onAction("reject", {
                      target_id: meta.page_id,
                      target_kind: "wiki_page",
                      reason: rejectReason.trim(),
                    });
                    setRejecting(false);
                    setRejectReason("");
                  }}
                  className="rounded bg-red-600 px-3 py-1 text-xs font-medium text-ink-inverse hover:bg-red-700 disabled:cursor-not-allowed disabled:opacity-50"
                  data-testid="wiki-reject-confirm"
                >
                  Reject page
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setRejecting(false);
                    setRejectReason("");
                  }}
                  className="rounded border border-line-strong px-3 py-1 text-xs font-medium text-ink-soft hover:bg-elevated"
                  data-testid="wiki-reject-cancel"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      {p.retracted && (
        <div
          className="mb-3 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs font-medium text-red-800"
          data-testid="wiki-retracted-banner"
        >
          ⚠ A source cited by this page has been retracted — its dependent claims are
          flagged and should be re-reviewed.
        </div>
      )}

      {view === "document" && p.html_url ? (
        <HtmlDocCard
          component={{
            type: "HtmlDocCard",
            id: "wiki-html-doc",
            props: { src: p.html_url, title: meta.title, read_only: true, derived: p.derived },
          }}
          onAction={onAction}
        />
      ) : bodyMd ? (
        <WikiBodyMarkdown
          body={bodyMd}
          onNavigate={navigateByTitle}
          resolveLink={resolveLink}
          renderCitation={(marker) => (
            <CitationBadge marker={marker} citation={citationMap.get(marker)} />
          )}
        />
      ) : (
        <p className="text-sm italic text-ink-muted">
          This page is a stub — no compiled revision yet.
        </p>
      )}

      {claims.length > 0 && (
        <section className="mt-5">
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-muted">
            Claims ({claims.length})
          </h4>
          <ul className="space-y-2">
            {claims.map((c) => (
              <li key={c.id}>
                <button
                  type="button"
                  onClick={() => selectClaim(c)}
                  className={`block w-full rounded text-left ${
                    highlightedClaimId === c.id ? "ring-2 ring-amber-400" : ""
                  }`}
                  data-testid={`wiki-claim-${c.id}`}
                >
                  <CardShell
                    subtitle={
                      <Pill tone={CONFIDENCE_TONE[c.confidence] ?? "slate"}>
                        <span data-testid="claim-confidence">{c.confidence}</span>
                      </Pill>
                    }
                  >
                    <p className="text-sm text-ink-soft">{c.text}</p>
                  </CardShell>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {wikilinks.length > 0 && (
        <section className="mt-5">
          <div className="mb-2 flex items-center justify-between">
            <h4 className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
              Links out ({wikilinks.length})
            </h4>
            {brokenLinks > 0 && !readOnly && (
              <button
                type="button"
                onClick={() => onAction("repair_links", {})}
                className="rounded border border-amber-400 bg-amber-50 px-2 py-0.5 text-xs font-medium text-amber-800 hover:bg-amber-100"
                data-testid="wiki-repair-links"
              >
                Repair {brokenLinks} broken link{brokenLinks === 1 ? "" : "s"}
              </button>
            )}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {wikilinks.map((l, i) =>
              l.dst_page_id ? (
                <button
                  key={`${l.dst_title}-${i}`}
                  type="button"
                  onClick={() => openPageId(l.dst_page_id as string)}
                  className="inline-flex items-center rounded border border-line-strong bg-elevated px-2 py-0.5 text-xs text-ink-soft hover:bg-line"
                  data-testid="wiki-linkout"
                >
                  [[{l.dst_title}]]
                  {l.occurrences > 1 && <span className="ml-1 text-ink-muted">×{l.occurrences}</span>}
                </button>
              ) : (
                <span
                  key={`${l.dst_title}-${i}`}
                  title="Unresolved link — no page with this title yet"
                  className="inline-flex items-center rounded border border-dashed border-amber-400 bg-amber-50 px-2 py-0.5 text-xs text-amber-700"
                  data-testid="wiki-linkout-broken"
                >
                  [[{l.dst_title}]]
                  {l.occurrences > 1 && <span className="ml-1 text-amber-500">×{l.occurrences}</span>}
                </span>
              ),
            )}
          </div>
        </section>
      )}
    </div>
  );
}

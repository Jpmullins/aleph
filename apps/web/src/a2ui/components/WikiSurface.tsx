import { useMemo, useState } from "react";

import { useWorkspaceUI } from "@/lib/workspace-ui";
import { WikiPageCard } from "./WikiPageCard";
import { Pill, SurfaceHeader, type RendererProps } from "./_shared";

/**
 * WP-4: the Wiki tab renders ONLY from the surface data model
 * (`{pages, open}`) streamed by the backend builder — no `useQuery`, no
 * polling, no fetch. Opening a page is an `open` A2UI action (routed through
 * the ledger-audited action router); the right panel re-streams with
 * `?page_id=`, which populates `open`. The rich reader card + inline
 * approve/reject/repair are the WP-4b reader tier; for now the body renders
 * through the bound markdown primitive and the reader is read-only.
 */
interface WikiPageSummary {
  id: string;
  title: string;
  slug: string;
  page_kind: string;
  is_stub: boolean;
  status: string;
  current_revision_id: string | null;
  last_compiled_at: string | null;
  freshness?: number | null;
  retracted?: boolean;
}

interface OpenPage {
  page_id: string;
  title: string;
  status: string;
  is_stub?: boolean;
  freshness?: number | null;
  volatility?: string;
  verified_at?: string | null;
  retracted?: boolean;
  revision: { body_md: string; revision_no: number; created_at: string } | null;
  claims: Array<{ id: string; text: string; confidence: string; section_anchor: string | null }>;
  citations: Array<Record<string, unknown>>;
  wikilinks_out: Array<{ dst_title: string; dst_page_id: string | null; occurrences: number }>;
  html_url?: string | null;
}

const STATUS_TONE: Record<string, "emerald" | "amber" | "slate"> = {
  approved: "emerald",
  draft: "amber",
  archived: "slate",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <Pill tone={STATUS_TONE[status] ?? "slate"}>
      <span data-testid="wiki-status-badge">{status}</span>
    </Pill>
  );
}

export function WikiSurface({ component, onAction }: RendererProps) {
  const pages = (component.props.pages as WikiPageSummary[] | undefined) ?? [];
  const open = (component.props.open as OpenPage | null | undefined) ?? null;
  const { setOpenPageId } = useWorkspaceUI();
  const [filter, setFilter] = useState("");
  const [draftsOnly, setDraftsOnly] = useState(false);

  const openPage = (id: string) => onAction("open", { target_id: id, target_kind: "wiki_page" });

  const draftCount = useMemo(() => pages.filter((p) => p.status === "draft").length, [pages]);

  const filtered = useMemo(() => {
    let list = pages;
    if (draftsOnly) list = list.filter((p) => p.status === "draft");
    if (!filter) return list;
    const needle = filter.toLowerCase();
    return list.filter((p) => p.title.toLowerCase().includes(needle));
  }, [pages, filter, draftsOnly]);

  const topicPages = filtered.filter((p) => p.page_kind !== "source");
  const sourcePages = filtered.filter((p) => p.page_kind === "source");

  if (open) {
    return (
      <div className="flex h-full flex-col">
        <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-surface px-3 py-2">
          <button
            type="button"
            onClick={() => setOpenPageId(null)}
            className="text-xs font-medium text-ink-muted hover:text-ink"
            data-testid="wiki-back"
          >
            ← Wiki
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          <WikiPageCard
            component={{
              type: "WikiPageCard",
              id: "wiki-reader",
              props: {
                body_md: open.revision?.body_md ?? "",
                claims: open.claims,
                citations: open.citations,
                wikilinks_out: open.wikilinks_out,
                html_url: open.html_url ?? null,
                retracted: open.retracted,
                page_meta: {
                  page_id: open.page_id,
                  title: open.title,
                  status: open.status,
                  is_stub: open.is_stub,
                  freshness: open.freshness == null ? null : String(open.freshness),
                },
              },
            }}
            onAction={onAction}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader title="Wiki" subtitle={`${pages.length} page${pages.length === 1 ? "" : "s"}`} />
      <div className="border-b border-line px-3 py-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter pages…"
          className="w-full rounded border border-line-strong px-2 py-1 text-xs focus:border-line-strong focus:outline-none"
          data-testid="wiki-filter"
        />
      </div>
      {draftCount > 0 && (
        <button
          type="button"
          onClick={() => setDraftsOnly((v) => !v)}
          className={`flex items-center justify-between gap-2 border-b border-amber-200 px-3 py-1.5 text-left text-xs ${
            draftsOnly ? "bg-amber-100 text-amber-900" : "bg-amber-50 text-amber-800 hover:bg-amber-100"
          }`}
          data-testid="wiki-needs-attention"
        >
          <span>
            ⚠ {draftCount} draft{draftCount === 1 ? "" : "s"} awaiting review
          </span>
          <span className="font-medium">{draftsOnly ? "Show all" : "Review"}</span>
        </button>
      )}
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {pages.length === 0 && <WikiEmptyState />}
        {topicPages.length > 0 && (
          <PageGroup label="Topic pages" pages={topicPages} onSelect={openPage} />
        )}
        {sourcePages.length > 0 && (
          <PageGroup label="Source pages" pages={sourcePages} onSelect={openPage} />
        )}
      </div>
    </div>
  );
}

function WikiEmptyState() {
  return (
    <div className="rounded-lg border border-dashed border-line-strong p-6 text-center">
      <p className="text-sm font-medium text-ink-soft">No wiki pages yet</p>
      <p className="mt-2 text-xs text-ink-muted">
        The wiki compiles from ingested sources. Click <strong>+ Upload source</strong> in the left
        panel to add a document — pages will appear here as the wiki agent compiles them.
      </p>
    </div>
  );
}

function PageGroup({
  label,
  pages,
  onSelect,
}: {
  label: string;
  pages: WikiPageSummary[];
  onSelect: (id: string) => void;
}) {
  return (
    <section>
      <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-ink-muted">
        {label} ({pages.length})
      </h4>
      <ul className="space-y-1.5">
        {pages.map((p) => (
          <li key={p.id}>
            <button
              type="button"
              onClick={() => onSelect(p.id)}
              className="block w-full rounded-md border border-line bg-surface px-3 py-2 text-left transition-colors hover:border-line-strong"
              data-testid={`wiki-page-${p.id}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium text-ink">{p.title}</span>
                <span className="flex items-center gap-1">
                  {p.retracted && (
                    <Pill tone="red">
                      <span data-testid={`wiki-row-retracted-${p.id}`}>⚠</span>
                    </Pill>
                  )}
                  {p.is_stub && <Pill tone="amber">stub</Pill>}
                  {p.status !== "approved" && <StatusBadge status={p.status} />}
                  {p.freshness != null && (
                    <Pill tone={p.freshness >= 60 ? "emerald" : p.freshness >= 30 ? "amber" : "red"}>
                      <span data-testid={`wiki-row-freshness-${p.id}`}>{p.freshness}</span>
                    </Pill>
                  )}
                </span>
              </div>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

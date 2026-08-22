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
  // Schema governance (aleph_wiki.schema), sent with the surface.
  category?: string | null;
  page_type?: string | null;
  tags?: string[];
  confidence?: string | null;
  contested?: boolean;
}

interface CategoryInfo {
  id: string;
  title: string;
  blurb: string;
}

interface WikiHealth {
  pages_scanned?: number;
  stubs_skipped?: number;
  total?: number;
  by_severity?: Record<string, number>;
  by_check?: Record<string, number>;
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

// Mirrors WikiService.STUB_PROMOTION_MENTIONS. Only used in explanatory copy —
// the rule itself is enforced server-side.
const STUB_PROMOTION_MENTIONS = 2;

const STATUS_TONE: Record<string, "good" | "warn" | "neutral"> = {
  approved: "good",
  draft: "warn",
  archived: "neutral",
  // A stub is a red link: a title something pointed at that nobody has written
  // yet. It is not a proposal and carries no claim, so it gets the quietest
  // tone we have — noticing it should take deliberate attention.
  stub: "neutral",
};

function StatusBadge({ status }: { status: string }) {
  return (
    <Pill tone={STATUS_TONE[status] ?? "neutral"}>
      <span data-testid="wiki-status-badge">{status}</span>
    </Pill>
  );
}

export function WikiSurface({ component, onAction }: RendererProps) {
  const rawPages = component.props.pages;
  const pages: WikiPageSummary[] = Array.isArray(rawPages)
    ? (rawPages as WikiPageSummary[])
    : [];
  const open = (component.props.open as OpenPage | null | undefined) ?? null;
  // `?? []` is not enough for a bound prop. A binding whose path is missing
  // from the data model resolves to a non-array — an empty object, or the
  // unresolved `{path}` descriptor itself — and `??` only catches null and
  // undefined, so the fallback never fires and iteration throws. Bound props
  // are data from a stream, not local state; they get a type check, not a
  // nullish default.
  const rawCategories = component.props.categories;
  const categories: CategoryInfo[] = Array.isArray(rawCategories)
    ? (rawCategories as CategoryInfo[])
    : [];
  const rawHealth = component.props.health;
  const health: WikiHealth =
    rawHealth && typeof rawHealth === "object" && !Array.isArray(rawHealth)
      ? (rawHealth as WikiHealth)
      : {};
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

  // Split on `is_stub`, not on status. `is_stub` is the durable fact — this
  // page has no content — while status is a workflow state that can be moved
  // independently: the corpus holds one stub somebody approved, which is still
  // an empty page and still belongs down here rather than among real ones.
  const written = filtered.filter((p) => !p.is_stub);
  const stubPages = filtered.filter((p) => p.is_stub);

  // Grouped by the schema's categories, in schema order — which is the order
  // somebody chose, not alphabetical. An uncategorised tail comes last rather
  // than being hidden: a page filed nowhere is present in the corpus and absent
  // from every hub, and showing it is what makes that visible.
  const groups = useMemo(() => {
    const byCategory = new Map<string, WikiPageSummary[]>();
    for (const page of written) {
      const key = page.category ?? "";
      const bucket = byCategory.get(key);
      if (bucket) bucket.push(page);
      else byCategory.set(key, [page]);
    }
    const ordered: Array<{ id: string; title: string; blurb: string; pages: WikiPageSummary[] }> =
      [];
    for (const cat of categories) {
      const inCat = byCategory.get(cat.id);
      if (inCat?.length) ordered.push({ ...cat, pages: inCat });
    }
    const loose = [
      ...(byCategory.get("") ?? []),
      ...categories.length
        ? [...byCategory.entries()]
            .filter(([k]) => k !== "" && !categories.some((c) => c.id === k))
            .flatMap(([, v]) => v)
        : [],
    ];
    if (loose.length) {
      ordered.push({
        id: "__uncategorised",
        title: "Uncategorised",
        blurb: "Filed under no category, so they appear in no hub",
        pages: loose,
      });
    }
    return ordered;
  }, [written, categories]);

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
    <div className="flex h-full flex-col" data-testid="wiki-surface">
      <SurfaceHeader title="Wiki" subtitle={`${pages.length} page${pages.length === 1 ? "" : "s"}`} />
      <div className="border-b border-line px-3 py-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter pages…"
          className="w-full border border-line-strong px-2 py-1 text-xs focus:border-accent focus:outline-none"
          data-testid="wiki-filter"
        />
      </div>
      {/* One line of corpus health from the lint's severity counts. Counts,
          not findings — the findings are their own read. */}
      {(health.total ?? 0) > 0 && (
        <div
          className="flex items-center justify-between gap-2 border-b border-line px-3 py-1.5 text-[11px] text-ink-muted"
          data-testid="wiki-health"
        >
          <span>
            {health.pages_scanned ?? 0} pages checked
            {(health.stubs_skipped ?? 0) > 0 ? ` · ${health.stubs_skipped} unwritten` : ""}
          </span>
          <span className="flex items-center gap-2 font-mono">
            {(health.by_severity?.broken ?? 0) > 0 && (
              <span className="text-[var(--state-bad)]">{health.by_severity?.broken} broken</span>
            )}
            {(health.by_severity?.structure ?? 0) > 0 && (
              <span>{health.by_severity?.structure} structure</span>
            )}
            {(health.by_severity?.quality ?? 0) > 0 && (
              <span>{health.by_severity?.quality} quality</span>
            )}
          </span>
        </div>
      )}
      {draftCount > 0 && (
        <button
          type="button"
          onClick={() => setDraftsOnly((v) => !v)}
          className={`flex items-center justify-between gap-2 border-b border-line px-3 py-1.5 text-left text-xs ${
            draftsOnly
              ? "bg-badge-warning-bg text-badge-warning-fg"
              : "bg-badge-warning-bg text-badge-warning-fg opacity-80 hover:opacity-100"
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
        {groups.map((g) => (
          <PageGroup
            key={g.id}
            label={g.title}
            blurb={g.blurb}
            pages={g.pages}
            onSelect={openPage}
          />
        ))}
        {stubPages.length > 0 && (
          <details className="group">
            <summary className="mb-1.5 cursor-pointer list-none text-xs font-semibold uppercase tracking-wider text-ink-muted hover:text-ink">
              <span className="inline-block w-3 transition-transform group-open:rotate-90">›</span>
              Unwritten ({stubPages.length})
            </summary>
            <p className="mb-2 pl-3 text-[11px] leading-relaxed text-ink-muted">
              Titles other pages link to that nobody has written yet. They become
              drafts on their own once {STUB_PROMOTION_MENTIONS} separate pages
              cite them.
            </p>
            <ul className="space-y-1 pl-3">
              {stubPages.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => openPage(p.id)}
                    className="block w-full truncate border-l border-line px-2 py-1 text-left text-xs text-ink-muted hover:border-accent hover:text-ink"
                    data-testid={`wiki-stub-${p.id}`}
                  >
                    {p.title}
                  </button>
                </li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}

function WikiEmptyState() {
  return (
    <div className="border border-dashed border-line-strong p-6 text-center">
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
  blurb,
  pages,
  onSelect,
}: {
  label: string;
  blurb?: string;
  pages: WikiPageSummary[];
  onSelect: (id: string) => void;
}) {
  return (
    <section>
      <h4 className="text-xs font-semibold uppercase tracking-wider text-ink-muted">
        {label} ({pages.length})
      </h4>
      {blurb ? <p className="mb-1.5 text-[11px] text-ink-muted">{blurb}</p> : <div className="mb-1.5" />}
      <ul className="space-y-1.5">
        {pages.map((p) => (
          <li key={p.id}>
            <button
              type="button"
              onClick={() => onSelect(p.id)}
              className="block w-full border border-line bg-surface px-3 py-2 text-left transition-colors hover:border-accent"
              data-testid={`wiki-page-${p.id}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium text-ink">{p.title}</span>
                <span className="flex items-center gap-1">
                  {p.retracted && (
                    <Pill tone="bad">
                      <span data-testid={`wiki-row-retracted-${p.id}`}>⚠</span>
                    </Pill>
                  )}
                  {p.is_stub && <Pill tone="warn">stub</Pill>}
                  {p.contested && (
                    <Pill tone="bad">
                      <span data-testid={`wiki-row-contested-${p.id}`}>contested</span>
                    </Pill>
                  )}
                  {/* Unset confidence is not the same as high, and the whole
                      point of recording it is that a reader can tell. */}
                  {!p.is_stub && !p.confidence && <Pill tone="neutral">unjudged</Pill>}
                  {p.confidence === "low" && <Pill tone="warn">low</Pill>}
                  {p.status !== "approved" && <StatusBadge status={p.status} />}
                  {p.freshness != null && (
                    <Pill tone={p.freshness >= 60 ? "good" : p.freshness >= 30 ? "warn" : "bad"}>
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

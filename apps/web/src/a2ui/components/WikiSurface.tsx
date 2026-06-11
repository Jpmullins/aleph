import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { renderChildCard, useSurface } from "../surface-context";
import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { useLiveSignals } from "@/hooks/live-signals";
import type { WikiLiveSignals } from "@/hooks/useWikiLiveSignals";
import { api } from "@/lib/api";
import { useWorkspaceUI } from "@/lib/workspace-ui";
import { CardShell, FeedbackButton, Pill, SurfaceHeader, type RendererProps } from "./_shared";

/** Is this page currently being compiled by an agent (by id or title)? */
function isCompiling(live: WikiLiveSignals, p: { id: string; title: string }): boolean {
  return live.compilingPages.has(p.id) || live.compilingPages.has(`title:${p.title}`);
}

interface WikiPageSummary {
  id: string;
  title: string;
  slug: string;
  page_kind: string; // "topic" | "source" | ...
  summary: string;
  is_stub: boolean;
  status: string;
  current_revision_id: string | null;
  last_compiled_at: string | null;
}

interface WikiPageDetail {
  page: WikiPageSummary;
  revision: { body_md: string; revision_no: number; created_at: string } | null;
  claims: Array<{ id: string; text: string; confidence: string; section_anchor: string | null }>;
  wikilinks_out: Array<{ dst_title: string; dst_page_id: string | null; occurrences: number }>;
}

const CONFIDENCE_TONE: Record<string, "emerald" | "amber" | "red" | "slate"> = {
  "well-supported": "emerald",
  well_supported: "emerald",
  contested: "amber",
  uncited: "red",
};

export function WikiSurface({ component, onAction }: RendererProps) {
  const { projectId } = useSurface();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const { openPageId, setOpenPageId } = useWorkspaceUI();

  // Honor external open requests (card "open" actions, agent navigation):
  // consume openPageId into the local selection, then clear it.
  useEffect(() => {
    if (openPageId) {
      setSelectedId(openPageId);
      setOpenPageId(null);
    }
  }, [openPageId, setOpenPageId]);

  // Live signals: instant index/page refresh + "✦ editing…" / "updated" presence
  // as agents write. One workspace-level subscription (LiveSignalsProvider);
  // freshness comes from the push stream, so the query below only keeps a slow
  // safety-net interval.
  const live = useLiveSignals();

  // Embedded A2UI cards (charts / tables / graphs / maps the agent placed
  // on this surface) render above the page browser, per spec §7.4 —
  // "inline charts/tables/maps render here where they belong."
  const embeds = component.children ?? [];

  const pages = useQuery<WikiPageSummary[]>({
    queryKey: ["wiki-pages", projectId],
    queryFn: () => api.get<WikiPageSummary[]>(`/v1/projects/${projectId}/wiki/pages`),
    // Event-driven now (the changes stream invalidates this on every write); the
    // interval is just a self-healing backstop if the stream drops.
    refetchInterval: 30_000,
  });

  const filtered = useMemo(() => {
    const list = pages.data ?? [];
    if (!filter) return list;
    const needle = filter.toLowerCase();
    return list.filter(
      (p) =>
        p.title.toLowerCase().includes(needle) ||
        p.summary.toLowerCase().includes(needle),
    );
  }, [pages.data, filter]);

  const topicPages = filtered.filter((p) => p.page_kind !== "source");
  const sourcePages = filtered.filter((p) => p.page_kind === "source");

  // Reading a page.
  if (selectedId) {
    const selectedPage = (pages.data ?? []).find((p) => p.id === selectedId);
    const compiling = selectedPage ? isCompiling(live, selectedPage) : false;
    return (
      <WikiPageReader
        projectId={projectId}
        pageId={selectedId}
        compiling={compiling}
        onBack={() => setSelectedId(null)}
      />
    );
  }

  return (
    <div className="flex h-full flex-col">
      <SurfaceHeader
        title="Wiki"
        subtitle={
          live.isAgentBuilding
            ? "✦ agent is building the wiki…"
            : pages.data
              ? `${pages.data.length} page${pages.data.length === 1 ? "" : "s"}`
              : undefined
        }
      />
      <div className="border-b border-slate-200 px-3 py-2">
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter pages…"
          className="w-full rounded border border-slate-300 px-2 py-1 text-xs focus:border-slate-500 focus:outline-none"
          data-testid="wiki-filter"
        />
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {embeds.length > 0 && (
          <section className="space-y-2" data-testid="wiki-embeds">
            {embeds.map((c) => (
              <div key={c.id}>{renderChildCard(c, onAction)}</div>
            ))}
          </section>
        )}

        {pages.isPending && <p className="text-sm text-slate-400">Loading wiki…</p>}

        {pages.isSuccess && pages.data.length === 0 && embeds.length === 0 && <WikiEmptyState />}

        {topicPages.length > 0 && (
          <PageGroup label="Topic pages" pages={topicPages} live={live} onSelect={setSelectedId} />
        )}
        {sourcePages.length > 0 && (
          <PageGroup label="Source pages" pages={sourcePages} live={live} onSelect={setSelectedId} />
        )}
      </div>
    </div>
  );
}

function WikiEmptyState() {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 p-6 text-center">
      <p className="text-sm font-medium text-slate-700">No wiki pages yet</p>
      <p className="mt-2 text-xs text-slate-500">
        The wiki compiles from ingested sources. Click{" "}
        <strong>+ Upload source</strong> in the left panel to add a document —
        you'll see the build progress in the <strong>Activity</strong> card at
        the top of the chat, and pages will appear here as the wiki agent
        compiles them.
      </p>
      <p className="mt-2 text-xs text-slate-400">
        Or use <code className="rounded bg-slate-100 px-1">/synthesize</code> in
        chat to research a topic and grow the wiki.
      </p>
    </div>
  );
}

function PageGroup({
  label,
  pages,
  live,
  onSelect,
}: {
  label: string;
  pages: WikiPageSummary[];
  live: WikiLiveSignals;
  onSelect: (id: string) => void;
}) {
  return (
    <section>
      <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-slate-500">
        {label} ({pages.length})
      </h4>
      <ul className="space-y-1.5">
        {pages.map((p) => {
          const compiling = isCompiling(live, p);
          const pulsing = live.recentlyCommitted.has(p.id);
          return (
            <li key={p.id}>
              <button
                type="button"
                onClick={() => onSelect(p.id)}
                className={`block w-full rounded-md border bg-white px-3 py-2 text-left transition-colors hover:border-slate-400 ${
                  pulsing
                    ? "border-emerald-400 bg-emerald-50"
                    : compiling
                      ? "border-sky-300"
                      : "border-slate-200"
                }`}
                data-testid={`wiki-page-${p.id}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-slate-900">{p.title}</span>
                  {compiling ? (
                    <Pill tone="sky">✦ editing…</Pill>
                  ) : pulsing ? (
                    <Pill tone="emerald">updated</Pill>
                  ) : (
                    p.is_stub && <Pill tone="amber">stub</Pill>
                  )}
                </div>
                {p.summary && (
                  <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">{p.summary}</p>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function WikiPageReader({
  projectId,
  pageId,
  compiling,
  onBack,
}: {
  projectId: string;
  pageId: string;
  compiling: boolean;
  onBack: () => void;
}) {
  const { surface } = useSurface();
  const detail = useQuery<WikiPageDetail>({
    queryKey: ["wiki-page", projectId, pageId],
    queryFn: () => api.get<WikiPageDetail>(`/v1/projects/${projectId}/wiki/pages/${pageId}`),
  });

  return (
    <div className="flex flex-col">
      <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-[var(--border-muted,#e2e8f0)] bg-[var(--surface-raised,#fff)] px-3 py-2">
        <button
          type="button"
          onClick={onBack}
          className="text-xs font-medium text-slate-500 hover:text-slate-900"
        >
          ← Wiki
        </button>
        {detail.data && (
          <span className="truncate text-sm font-semibold text-slate-900">
            {detail.data.page.title}
          </span>
        )}
        {detail.data && (
          <span className="ml-auto">
            <FeedbackButton
              projectId={projectId}
              targetKind="wiki_page"
              targetId={pageId}
              surface={surface}
            />
          </span>
        )}
      </div>
      <div className="p-4">
        {compiling && (
          <div
            className="mb-3 flex items-center gap-2 rounded-md border border-sky-200 bg-sky-50 px-3 py-1.5 text-xs text-sky-700"
            data-testid="wiki-editing-banner"
          >
            <span className="animate-pulse">✦</span> An agent is editing this page — it will
            refresh as soon as the changes land.
          </div>
        )}
        {detail.isPending && <p className="text-sm text-slate-400">Loading page…</p>}
        {detail.isError && <p className="text-sm text-red-700">Failed to load page.</p>}
        {detail.data && (
          <>
            {detail.data.revision ? (
              <WikiBodyMarkdown body={detail.data.revision.body_md} />
            ) : (
              <p className="text-sm italic text-slate-500">
                This page is a stub — no compiled revision yet.
              </p>
            )}

            {detail.data.claims.length > 0 && (
              <section className="mt-5">
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
                  Claims ({detail.data.claims.length})
                </h4>
                <ul className="space-y-2">
                  {detail.data.claims.map((c) => (
                    <li key={c.id}>
                      <CardShell subtitle={<Pill tone={CONFIDENCE_TONE[c.confidence] ?? "slate"}>{c.confidence}</Pill>}>
                        <p className="text-sm text-slate-700">{c.text}</p>
                      </CardShell>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {detail.data.wikilinks_out.length > 0 && (
              <section className="mt-5">
                <h4 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">
                  Links out ({detail.data.wikilinks_out.length})
                </h4>
                <div className="flex flex-wrap gap-1.5">
                  {detail.data.wikilinks_out.map((l, i) => (
                    <span
                      key={`${l.dst_title}-${i}`}
                      className="inline-flex items-center rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700"
                    >
                      [[{l.dst_title}]]
                      {l.occurrences > 1 && (
                        <span className="ml-1 text-slate-400">×{l.occurrences}</span>
                      )}
                    </span>
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}

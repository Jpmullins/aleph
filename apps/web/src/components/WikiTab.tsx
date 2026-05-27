import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";
import { api } from "@/lib/api";

interface WikiPageSummary {
  id: string;
  title: string;
  slug: string;
  page_kind: string;
  status: string;
  is_stub: boolean;
  current_revision_id: string | null;
  last_compiled_at: string | null;
}

interface WikiPageDetail {
  page: WikiPageSummary;
  revision: { id: string; revision_no: number; body_md: string; summary: string } | null;
  claims: Array<{ id: string; text: string; confidence: string; section_anchor: string | null }>;
  wikilinks_out: Array<{ dst_title: string; dst_page_id: string | null; occurrences: number }>;
}

interface Props {
  projectId: string;
}

export function WikiTab({ projectId }: Props) {
  const [selectedPageId, setSelectedPageId] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const pages = useQuery<WikiPageSummary[]>({
    queryKey: ["wiki-pages", projectId],
    queryFn: () => api.get<WikiPageSummary[]>(`/v1/projects/${projectId}/wiki/pages`),
    refetchInterval: 5_000,
  });

  useEffect(() => {
    if (!selectedPageId && pages.data && pages.data.length > 0) {
      setSelectedPageId(pages.data[0].id);
    }
  }, [pages.data, selectedPageId]);

  const filtered = pages.data?.filter((p) => {
    if (!search.trim()) return true;
    return p.title.toLowerCase().includes(search.toLowerCase());
  });

  const detail = useQuery<WikiPageDetail>({
    queryKey: ["wiki-page", projectId, selectedPageId],
    queryFn: () =>
      api.get<WikiPageDetail>(
        `/v1/projects/${projectId}/wiki/pages/${selectedPageId}`,
      ),
    enabled: !!selectedPageId,
  });

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-200 px-4 py-3">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Filter pages…"
          className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-xs"
        />
      </div>
      <div className="flex min-h-0 flex-1">
        <aside className="w-48 overflow-y-auto border-r border-slate-200 p-3 text-xs">
          {pages.isPending && <p className="text-slate-500">Loading…</p>}
          {pages.isSuccess && filtered?.length === 0 && (
            <p className="text-slate-500">No pages yet.</p>
          )}
          <ul className="space-y-1">
            {filtered?.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  onClick={() => setSelectedPageId(p.id)}
                  className={
                    "block w-full rounded px-2 py-1 text-left " +
                    (selectedPageId === p.id
                      ? "bg-slate-900 text-white"
                      : "text-slate-700 hover:bg-slate-100")
                  }
                >
                  <div className="truncate font-medium">{p.title}</div>
                  <div
                    className={
                      "text-[10px] uppercase tracking-wider " +
                      (selectedPageId === p.id ? "text-slate-300" : "text-slate-400")
                    }
                  >
                    {p.page_kind}
                    {p.is_stub ? " · stub" : ""}
                  </div>
                </button>
              </li>
            ))}
          </ul>
        </aside>
        <section className="flex-1 overflow-y-auto p-6">
          {!selectedPageId && (
            <p className="text-sm text-slate-500">Select a page to read.</p>
          )}
          {detail.isPending && selectedPageId && (
            <p className="text-sm text-slate-500">Loading page…</p>
          )}
          {detail.data?.revision && (
            <>
              <WikiBodyMarkdown
                body={detail.data.revision.body_md}
                onNavigate={(target) => {
                  const match = pages.data?.find(
                    (p) => p.title === target || p.slug === target,
                  );
                  if (match) setSelectedPageId(match.id);
                }}
              />
              {detail.data.claims.length > 0 && (
                <section className="mt-8 border-t border-slate-200 pt-4">
                  <h3 className="text-sm font-semibold text-slate-900">Claims</h3>
                  <ul className="mt-2 space-y-2 text-xs">
                    {detail.data.claims.map((c) => (
                      <li key={c.id} className="rounded bg-slate-50 p-2">
                        <span className="font-medium text-slate-700">
                          [{c.confidence}]
                        </span>{" "}
                        {c.text}
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}

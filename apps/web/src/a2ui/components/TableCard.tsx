import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { useSurface } from "../surface-context";
import { api } from "@/lib/api";
import { CardShell, Pill, type RendererProps } from "./_shared";

interface ObservationsPage {
  rows: Array<Record<string, unknown>>;
  columns: Array<{ name: string; label?: string; type?: string }>;
  total: number;
}

export function TableCard({ component }: RendererProps) {
  const { projectId } = useSurface();
  const p = component.props as {
    dataset_version_id?: string | null;
    title?: string;
    columns?: Array<{ name: string; label?: string }>;
    rows?: Array<Record<string, unknown>>;
    _placeholder?: boolean;
  };
  const [sortBy, setSortBy] = useState<string | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [filter, setFilter] = useState("");

  const dataQuery = useQuery<ObservationsPage>({
    queryKey: ["dataset-rows", projectId, p.dataset_version_id],
    queryFn: () =>
      api.get<ObservationsPage>(
        `/v1/projects/${projectId}/datasets/versions/${p.dataset_version_id}/observations?limit=500`,
      ),
    enabled: !!p.dataset_version_id && !p.rows,
  });

  const columns = p.columns ?? dataQuery.data?.columns ?? [];
  const rows = p.rows ?? dataQuery.data?.rows ?? [];

  const filtered = useMemo(() => {
    let xs = rows;
    if (filter) {
      const needle = filter.toLowerCase();
      xs = xs.filter((r) =>
        Object.values(r).some((v) => String(v ?? "").toLowerCase().includes(needle)),
      );
    }
    if (sortBy) {
      xs = [...xs].sort((a, b) => {
        const av = a[sortBy];
        const bv = b[sortBy];
        if (av === bv) return 0;
        if (av == null) return 1;
        if (bv == null) return -1;
        const cmp = (av as number | string) < (bv as number | string) ? -1 : 1;
        return sortDir === "asc" ? cmp : -cmp;
      });
    }
    return xs;
  }, [rows, filter, sortBy, sortDir]);

  if (p._placeholder || (rows.length === 0 && !p.dataset_version_id && !dataQuery.isPending)) {
    return (
      <CardShell title={p.title || "Table"} subtitle={<Pill tone="slate">No data bound</Pill>}>
        <p className="text-xs text-slate-500">Bind a DatasetVersion or pass rows to render.</p>
      </CardShell>
    );
  }

  const headers = columns.length > 0
    ? columns
    : (rows[0] ? Object.keys(rows[0]).map((n) => ({ name: n, label: n })) : []);

  return (
    <CardShell title={p.title || "Table"} subtitle={`${filtered.length} of ${rows.length} rows`}>
      <input
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter…"
        className="mb-2 w-full rounded border border-slate-200 px-2 py-1 text-xs focus:border-slate-500 focus:outline-none"
      />
      {dataQuery.isPending && rows.length === 0 && (
        <p className="text-xs text-slate-400">Loading rows…</p>
      )}
      <div className="max-h-72 overflow-auto">
        <table className="w-full border-collapse text-xs">
          <thead className="sticky top-0 bg-slate-50 text-left">
            <tr>
              {headers.map((c) => (
                <th
                  key={c.name}
                  className="cursor-pointer border-b border-slate-200 px-2 py-1.5 font-medium text-slate-700 hover:bg-slate-100"
                  onClick={() => {
                    if (sortBy === c.name) setSortDir(sortDir === "asc" ? "desc" : "asc");
                    else {
                      setSortBy(c.name);
                      setSortDir("asc");
                    }
                  }}
                >
                  {c.label ?? c.name}
                  {sortBy === c.name && <span className="ml-1 text-[10px]">{sortDir === "asc" ? "▲" : "▼"}</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => (
              <tr key={i} className="border-b border-slate-100 hover:bg-slate-50">
                {headers.map((c) => (
                  <td key={c.name} className="px-2 py-1 text-slate-700">
                    {formatCell(r[c.name])}
                  </td>
                ))}
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr>
                <td colSpan={headers.length} className="px-2 py-4 text-center text-slate-400">
                  No rows match.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </CardShell>
  );
}

function formatCell(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

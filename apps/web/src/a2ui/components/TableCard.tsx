import { CardShell, type RendererProps } from "./_shared";

export function TableCard({ component }: RendererProps) {
  const p = component.props as {
    title?: string;
    columns?: Array<{ name: string; label?: string }>;
    rows?: Array<Record<string, unknown>>;
  };
  if (!p.rows || p.rows.length === 0) {
    return (
      <CardShell title={p.title || "Table"}>
        <p className="text-xs text-slate-500">No rows.</p>
      </CardShell>
    );
  }
  const cols = p.columns ?? Object.keys(p.rows[0]).map((name) => ({ name }));
  return (
    <CardShell title={p.title || "Table"}>
      <table className="w-full text-xs">
        <thead>
          <tr>
            {cols.map((c) => (
              <th
                key={c.name}
                className="border-b border-slate-200 px-2 py-1 text-left text-[10px] uppercase text-slate-500"
              >
                {c.label ?? c.name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {p.rows.slice(0, 100).map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td key={c.name} className="border-b border-slate-100 px-2 py-1">
                  {String((r as Record<string, unknown>)[c.name] ?? "")}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </CardShell>
  );
}

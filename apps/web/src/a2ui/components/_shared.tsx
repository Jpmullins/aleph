import type { A2UIComponent } from "../catalog";

export interface RendererProps {
  component: A2UIComponent;
  onAction: (action: string, params: Record<string, unknown>) => void;
}

export function Pill({
  tone = "slate",
  children,
}: {
  tone?: "slate" | "amber" | "emerald" | "red" | "sky";
  children: React.ReactNode;
}) {
  const cls =
    tone === "amber"
      ? "bg-amber-100 text-amber-900"
      : tone === "emerald"
        ? "bg-emerald-100 text-emerald-900"
        : tone === "red"
          ? "bg-red-100 text-red-900"
          : tone === "sky"
            ? "bg-sky-100 text-sky-900"
            : "bg-slate-100 text-slate-700";
  return (
    <span
      className={`inline-flex items-center rounded px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${cls}`}
    >
      {children}
    </span>
  );
}

export function CardShell({
  title,
  subtitle,
  children,
}: {
  title?: string;
  subtitle?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
      {title && (
        <div className="mb-1 text-sm font-semibold text-slate-900">{title}</div>
      )}
      {subtitle && <div className="mb-2 text-xs text-slate-500">{subtitle}</div>}
      {children}
    </div>
  );
}

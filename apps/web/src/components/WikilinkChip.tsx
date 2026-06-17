interface Props {
  text: string;
  onNavigate?: (slugOrTitle: string) => void;
  /** True when the wikilink does not resolve to an existing page. */
  broken?: boolean;
}

/**
 * Renders a [[wikilink]] chip. Click navigates to the referenced page via the
 * Wiki tab. A `broken` link (no resolved target page) renders visibly distinct
 * and is not clickable. Used both inside MD bodies and inside chat.
 */
export function WikilinkChip({ text, onNavigate, broken = false }: Props) {
  if (broken) {
    return (
      <span
        className="mx-0.5 inline-flex items-center rounded border border-dashed border-amber-400 bg-amber-50 px-1.5 py-0.5 text-xs font-medium text-amber-700"
        title="Unresolved link — no page with this title yet"
        data-testid="wikilink-broken"
      >
        [[{text}]]
      </span>
    );
  }
  return (
    <button
      type="button"
      className="mx-0.5 inline-flex items-center rounded border border-slate-300 bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-200"
      onClick={() => onNavigate?.(text)}
      data-testid="wikilink-chip"
    >
      [[{text}]]
    </button>
  );
}

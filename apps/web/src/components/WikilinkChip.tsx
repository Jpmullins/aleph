interface Props {
  /** What the reader sees — the display half of `[[target|display]]`. */
  text: string;
  /**
   * What the link points at. Distinct from `text` because Obsidian's pipe form
   * lets a page be linked under a name it is not filed under: the generated
   * hubs emit `[[logging-recovery-hub|Logging and Recovery Hub]]`, and
   * navigating to the display half would look up a page that does not exist
   * under that name. Defaults to `text` for the plain `[[title]]` form.
   */
  target?: string;
  onNavigate?: (slugOrTitle: string) => void;
  /** True when the wikilink does not resolve to an existing page. */
  broken?: boolean;
}

/**
 * Renders a [[wikilink]] chip. Click navigates to the referenced page via the
 * Wiki tab. A `broken` link (no resolved target page) renders visibly distinct
 * and is not clickable. Used both inside MD bodies and inside chat.
 */
export function WikilinkChip({ text, target, onNavigate, broken = false }: Props) {
  const destination = target ?? text;
  if (broken) {
    return (
      <span
        className="mx-0.5 inline-flex items-center rounded border border-dashed border-amber-400 bg-amber-50 px-1.5 py-0.5 text-xs font-medium text-amber-700"
        title={
          destination === text
            ? "Unresolved link — no page with this title yet"
            : `Unresolved link — no page called "${destination}" yet`
        }
        data-testid="wikilink-broken"
      >
        [[{text}]]
      </span>
    );
  }
  return (
    <button
      type="button"
      className="mx-0.5 inline-flex items-center rounded border border-line-strong bg-elevated px-1.5 py-0.5 text-xs font-medium text-ink-soft hover:bg-line"
      onClick={() => onNavigate?.(destination)}
      data-testid="wikilink-chip"
    >
      {text}
    </button>
  );
}

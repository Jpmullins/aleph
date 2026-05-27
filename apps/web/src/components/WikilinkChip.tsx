interface Props {
  text: string;
  onNavigate?: (slugOrTitle: string) => void;
}

/**
 * Renders a [[wikilink]] chip. Click navigates to the referenced page
 * via the Wiki tab. Used both inside MD bodies and inside chat in Inc 2.
 */
export function WikilinkChip({ text, onNavigate }: Props) {
  return (
    <button
      type="button"
      className="mx-0.5 inline-flex items-center rounded border border-slate-300 bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-700 hover:bg-slate-200"
      onClick={() => onNavigate?.(text)}
    >
      [[{text}]]
    </button>
  );
}

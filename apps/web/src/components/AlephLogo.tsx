/**
 * Aleph brand mark — the Hebrew aleph (א), a nod to Borges' "El Aleph" (the
 * point that contains all points) and Cantor's ℵ (aleph) notation for the
 * infinite. Used in the landing header and workspace.
 */
interface Props {
  /** Mark size in px. */
  size?: number;
  /** Show the "Aleph" wordmark beside the mark. */
  showWordmark?: boolean;
  /** Optional tagline under the wordmark. */
  tagline?: string;
}

export function AlephLogo({ size = 40, showWordmark = true, tagline }: Props) {
  return (
    <div className="flex items-center gap-3">
      <span
        aria-hidden
        className="grid shrink-0 place-items-center rounded-xl font-serif leading-none text-white shadow-sm"
        style={{
          width: size,
          height: size,
          fontSize: size * 0.62,
          // Accent gradient (design-token accent #f97316 → deeper amber).
          background: "linear-gradient(135deg, var(--accent, #f97316), #b45309)",
        }}
      >
        א
      </span>
      {showWordmark && (
        <span className="flex flex-col leading-tight">
          <span className="font-serif text-2xl font-semibold tracking-tight text-[var(--text-primary,#0f172a)]">
            Aleph
          </span>
          {tagline && (
            <span className="text-xs text-[var(--text-muted,#64748b)]">{tagline}</span>
          )}
        </span>
      )}
    </div>
  );
}

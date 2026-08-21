/**
 * The astrolabe — Aleph's mark, in two weights.
 *
 * An astrolabe is an instrument for fixing your position by measuring where
 * things are relative to each other, which is what this product does with
 * claims. The mark is drawn rather than generated so it scales, recolours with
 * the theme, and prints.
 *
 * Two drawings, because one cannot serve both an about screen and a browser
 * tab. The graduated ring is the emblem's whole character and turns to a grey
 * smudge below roughly 40px, so the small weight drops it entirely and doubles
 * the strokes — the ring-and-diagonal silhouette is what makes it recognisable,
 * and that is all `mark` keeps.
 *
 * `currentColor` throughout: one file, both themes, no light/dark variants.
 */

interface Props {
  /** Rendered size in px. Below 40 the emblem is unreadable; use `mark`. */
  size?: number;
  /** `mark` for chrome and favicons, `emblem` where there is room to breathe. */
  variant?: "mark" | "emblem";
  className?: string;
  title?: string;
}

/** 60 graduations, major every 5 — the detail that reads as an instrument. */
function graduations(): React.ReactElement[] {
  const ticks: React.ReactElement[] = [];
  for (let i = 0; i < 60; i += 1) {
    const a = (2 * Math.PI * i) / 60;
    const major = i % 5 === 0;
    const rOut = 45;
    const rIn = major ? 37.4 : 39;
    ticks.push(
      <line
        key={i}
        x1={50 + rOut * Math.cos(a)}
        y1={50 + rOut * Math.sin(a)}
        x2={50 + rIn * Math.cos(a)}
        y2={50 + rIn * Math.sin(a)}
        strokeWidth={major ? 1.35 : 0.9}
      />,
    );
  }
  return ticks;
}

export function AlephLogo({ size = 20, variant = "mark", className, title }: Props) {
  const label = title ?? "Aleph";

  if (variant === "emblem") {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 100 100"
        className={className}
        role="img"
        aria-label={label}
        fill="none"
        stroke="currentColor"
      >
        <circle cx="50" cy="50" r="45.5" strokeWidth="1.6" />
        <circle cx="50" cy="50" r="38" strokeWidth="1.1" />
        <g stroke="currentColor">{graduations()}</g>
        <circle cx="50" cy="50" r="30" strokeWidth="1.4" />
        <circle cx="50" cy="50" r="20" strokeWidth="1.4" />
        {/* the alidade — tapered, so it reads as a rule rather than a slash */}
        <polygon points="79.5,17.5 83,21 55,53 51,49" fill="currentColor" stroke="none" />
        <polygon points="21,79.5 17.5,76 47,47 51,51" fill="currentColor" stroke="none" />
        <circle cx="19.5" cy="80.5" r="4.2" strokeWidth="1.8" />
        <circle cx="50" cy="50" r="6.4" fill="currentColor" stroke="none" />
      </svg>
    );
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 100 100"
      className={className}
      role="img"
      aria-label={label}
      fill="none"
      stroke="currentColor"
    >
      <circle cx="50" cy="50" r="34" strokeWidth="7" />
      <circle cx="50" cy="50" r="17" strokeWidth="6" />
      <line x1="29" y1="71" x2="71" y2="29" strokeWidth="8" strokeLinecap="square" />
      <circle cx="50" cy="50" r="7" fill="currentColor" stroke="none" />
    </svg>
  );
}

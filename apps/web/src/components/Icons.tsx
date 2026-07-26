/**
 * Inline stroke icons for the rail and chrome.
 *
 * Replaces the emoji glyphs the workspace used for its controls (⚙ 🗒 🔔 ● ▲ ▼
 * ✕ 👎 ◻ ⏳ ✓). Emoji are unthemeable — they ignore `currentColor`, so they
 * cannot follow the light/dark token set — they render differently on every
 * platform, and they sit at whatever size the font decides rather than the type
 * scale.
 *
 * Inline SVG so the app has no icon font and no CDN dependency, and every glyph
 * inherits `currentColor` and the surrounding size. Pattern borrowed from
 * benchmesh's `icons.tsx`; the shapes are drawn for Aleph's own surfaces.
 */
import type { ReactNode } from "react";

type IconProps = { size?: number; className?: string };

function Svg({ children, size = 18, className }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export const Icons = {
  /** Wiki — an open book. */
  wiki: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M12 6.5C10.5 5 8.5 4.5 4 4.5v13c4.5 0 6.5.5 8 2 1.5-1.5 3.5-2 8-2v-13c-4.5 0-6.5.5-8 2z" />
      <path d="M12 6.5v13" />
    </Svg>
  ),
  /** Library — stacked source documents. */
  library: (p: IconProps = {}) => (
    <Svg {...p}>
      <rect x="3.5" y="3.5" width="5" height="17" rx="1" />
      <rect x="10" y="3.5" width="5" height="17" rx="1" />
      <path d="M17.5 5.2l3 16.2" />
    </Svg>
  ),
  /** Notes — a page with lines. */
  notes: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M6 3.5h9l4 4v13H6z" />
      <path d="M14.5 3.5V8H19" />
      <path d="M9 12.5h7M9 16h5" />
    </Svg>
  ),
  /** Hypotheses — a balance, for weighing evidence. */
  hypotheses: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M12 4v16M7 20h10" />
      <path d="M4.5 8h15" />
      <path d="M4.5 8L2 13.5h5zM19.5 8L17 13.5h5z" />
    </Svg>
  ),
  /** Briefs — a pinned card. */
  briefs: (p: IconProps = {}) => (
    <Svg {...p}>
      <rect x="4" y="4.5" width="16" height="15" rx="2" />
      <path d="M8 9h8M8 13h5" />
    </Svg>
  ),
  settings: (p: IconProps = {}) => (
    <Svg {...p}>
      <circle cx="12" cy="12" r="3" />
      <path d="M12 2.8l1.2 2.6 2.8-.6 1 2.7 2.7 1-.6 2.8 2.6 1.2-2.6 1.2.6 2.8-2.7 1-1 2.7-2.8-.6L12 21.2l-1.2-2.6-2.8.6-1-2.7-2.7-1 .6-2.8L2.3 11.5l2.6-1.2-.6-2.8 2.7-1 1-2.7 2.8.6z" />
    </Svg>
  ),
  ledger: (p: IconProps = {}) => (
    <Svg {...p}>
      <rect x="4.5" y="3.5" width="15" height="17" rx="1.5" />
      <path d="M8 8h8M8 12h8M8 16h5" />
    </Svg>
  ),
  bell: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 0 1-3.46 0" />
    </Svg>
  ),
  profile: (p: IconProps = {}) => (
    <Svg {...p}>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M4.5 20a7.5 7.5 0 0 1 15 0" />
    </Svg>
  ),
  upload: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M12 16V4" />
      <path d="M7.5 8.5L12 4l4.5 4.5" />
      <path d="M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16" />
    </Svg>
  ),
  back: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M14 6l-6 6 6 6" />
    </Svg>
  ),
  plus: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M12 5v14M5 12h14" />
    </Svg>
  ),
  close: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M6 6l12 12M18 6L6 18" />
    </Svg>
  ),
  sun: (p: IconProps = {}) => (
    <Svg {...p}>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M19.1 4.9l-1.4 1.4M6.3 17.7l-1.4 1.4" />
    </Svg>
  ),
  moon: (p: IconProps = {}) => (
    <Svg {...p}>
      <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5z" />
    </Svg>
  ),
} as const;

export type IconName = keyof typeof Icons;

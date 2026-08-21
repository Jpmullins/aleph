import type { Config } from "tailwindcss";

/**
 * Content globs only. Theme lives in CSS.
 *
 * This file used to also declare `fontFamily`, which made it a SECOND source of
 * truth alongside the `@theme inline` block in `src/styles.css` — and the two
 * disagreed: the tokens said Public Sans while this said Inter, and Inter won,
 * so the type system we designed was never actually applied. Same defect class
 * as the two catalogs and the two pane registries.
 *
 * Tailwind v4 reads its theme from CSS. Colours, fonts and radii are declared
 * once in `styles.css`, resolved from the custom properties in
 * `styles/tokens.css`, so a value cannot be right in one file and wrong here.
 */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
} satisfies Config;

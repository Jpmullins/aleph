/**
 * The confidence vocabulary, on the client side.
 *
 * The canonical definition is `aleph_core.confidence.Confidence` (Python). This
 * file is the TypeScript half of the same closed set, and
 * `scripts/check-confidence-vocabulary.sh` fails the build if the two lists
 * differ — along with the A2UI catalog's `ClaimCard.confidence` enum and the
 * HTML compiler's badge classes, which are the other two readers.
 *
 * Why a closed union rather than `string`: `ClaimCard` used to branch on four
 * literals out of a possible seven and fall through to grey for the rest. The
 * three the engine could actually emit — `refuted`, `abandoned`,
 * `under_investigation` — all landed in that fallthrough, so a claim the
 * evidence had DISPROVED rendered identically to one nobody had looked at. The
 * `never` check in `confidenceTone` makes adding a state without deciding how
 * it looks a compile error.
 *
 * The set is kept in the engine's own order (weakest evidence first, then the
 * terminal states), because a reader diffing this against the Python enum
 * should see a real difference and not a reordering.
 */

export const CONFIDENCE = [
  "under_investigation",
  "weakly_supported",
  "well_supported",
  "contested",
  "refuted",
  "abandoned",
] as const;

export type Confidence = (typeof CONFIDENCE)[number];

export type PillTone = "slate" | "amber" | "emerald" | "red" | "sky" | "violet";

/**
 * True when `value` is a member of the vocabulary.
 *
 * Needed because the value arrives over SSE as a plain string: the surface
 * producer is validated against the catalog schema, but a pane must still cope
 * with a row written before the vocabulary was unified without crashing.
 */
export function isConfidence(value: unknown): value is Confidence {
  return typeof value === "string" && (CONFIDENCE as readonly string[]).includes(value);
}

/**
 * Badge colour for a confidence state.
 *
 * Exhaustive by construction: the `default` branch assigns to a `never`, so a
 * seventh member added to `CONFIDENCE` without a case here fails
 * `pnpm -C apps/web build` rather than rendering as slate.
 */
export function confidenceTone(value: Confidence): PillTone {
  switch (value) {
    case "well_supported":
      return "emerald";
    case "weakly_supported":
      return "sky";
    case "contested":
      return "amber";
    case "refuted":
      return "red";
    case "abandoned":
      return "violet";
    case "under_investigation":
      return "slate";
    default: {
      const unhandled: never = value;
      return unhandled;
    }
  }
}

/** Human-facing label. The stored value is snake_case; a badge is not. */
export function confidenceLabel(value: string): string {
  return value.replace(/_/g, " ");
}

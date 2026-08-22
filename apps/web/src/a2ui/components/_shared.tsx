import { useState, type ReactNode } from "react";

import type { A2UIComponent } from "../catalog";
import type { PillTone } from "../confidence";

export interface RendererProps {
  component: A2UIComponent;
  onAction: (action: string, params: Record<string, unknown>) => void;
}

/**
 * Amended rule 8 (WP-4c): an interactive/document artifact iframe may load ONLY
 * a project-scoped asset streaming route under the principal boundary — never a
 * data: URI, an external origin, or an arbitrary agent-supplied URL. These
 * routes serve their bytes with a server `Content-Security-Policy: sandbox`, so
 * they can never execute in the API page context. `HtmlDocCard` /
 * `HtmlFrameCard` call this on their bound `src` and refuse to mount otherwise.
 *
 * Accepted (all principal-boundary, CSP-sandboxed):
 *   /v1/projects/{id}/assets/rendered/{id}
 *   /v1/projects/{id}/assets/artifact-version/{id}
 *   /v1/projects/{id}/wiki/pages/{id}/html   (deterministic compiled wiki doc)
 */
const _ASSET_SRC_RE =
  /^\/v1\/projects\/[^/]+\/(assets\/(rendered|artifact-version)\/[^/]+|wiki\/pages\/[^/]+\/html)\/?$/;

export function isSandboxedAssetSrc(src: unknown): src is string {
  return typeof src === "string" && _ASSET_SRC_RE.test(src);
}

/**
 * Badge classes per tone.
 *
 * `Record<PillTone, string>` and not `Record<string, string>`, which is what it
 * was. The looser type is why the tone vocabulary could be renamed off Tailwind
 * hue names (`tone="emerald"`) to state names without a single compile error
 * while every key here still said `emerald:` — `cls[tone]` would have been
 * `undefined` for all six tones and every badge in the app would have quietly
 * rendered idle grey. Keyed by the union, a missed key is a build failure.
 *
 * The values named their token with a hardcoded pale-cream literal after the
 * comma — the arbitrary-value form of a var() fallback. The literal is what
 * renders whenever the token is absent, in whichever theme that happens to be,
 * and on the day
 * `--badge-warning-bg` was undefined every "draft" badge painted itself bright
 * cream on the near-black ground with nothing reporting it. tokens.css defines
 * all five pairs for both themes and styles.css maps them into the Tailwind
 * theme, so these are plain semantic utilities with nothing to fall back to.
 */
const PILL_CLASS: Record<PillTone, string> = {
  neutral: "bg-badge-idle-bg text-badge-idle-fg",
  info: "bg-badge-running-bg text-badge-running-fg",
  good: "bg-badge-completed-bg text-badge-completed-fg",
  warn: "bg-badge-warning-bg text-badge-warning-fg",
  bad: "bg-badge-failed-bg text-badge-failed-fg",
  // `inactive` and `neutral` currently render identically, which is a real gap
  // rather than a tidy-up: `confidenceTone` maps `abandoned` to `inactive` and
  // `under_investigation` to `neutral`, so a hypothesis somebody GAVE UP ON
  // looks exactly like one nobody has assessed. That is the same defect
  // `confidence.ts`'s own docstring records for the four-literal switch. It
  // needs a sixth badge pair in tokens.css, which is a palette decision, not a
  // rename — kept distinct here so the day someone adds that pair, one line
  // changes and every call site is already correct.
  inactive: "bg-badge-idle-bg text-badge-idle-fg",
};

export function Pill({ tone = "neutral", children }: { tone?: PillTone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${PILL_CLASS[tone]}`}
    >
      {children}
    </span>
  );
}

export function CardShell({
  title,
  subtitle,
  children,
  actions,
  footer,
}: {
  title?: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="border border-line bg-surface p-3">
      {(title || actions) && (
        <div className="mb-1 flex items-start justify-between gap-2">
          {title && (
            <div className="text-sm font-semibold text-ink">{title}</div>
          )}
          {actions && <div className="flex items-center gap-1">{actions}</div>}
        </div>
      )}
      {subtitle && <div className="mb-2 text-xs text-ink-muted">{subtitle}</div>}
      {children}
      {footer && (
        <div className="mt-2 border-t border-line pt-2 text-[11px] text-ink-muted">
          {footer}
        </div>
      )}
    </div>
  );
}

export function SurfaceHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-b border-line px-4 py-3">
      <div>
        <h3 className="text-sm font-semibold uppercase tracking-wider text-ink-soft">
          {title}
        </h3>
        {subtitle && <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

export type FeedbackSignal =
  | "thumbs_up"
  | "thumbs_down"
  | "marked_wrong"
  | "misleading"
  | "false_positive"
  | "excellent"
  | "note_only";

export type FeedbackTargetKind =
  | "claim"
  | "source"
  | "chart"
  | "finding"
  | "hypothesis"
  | "assistant_message"
  | "wiki_page";

interface FeedbackButtonProps {
  targetKind: FeedbackTargetKind;
  targetId: string;
  surface?: string;
  /**
   * Route the feedback through the ledger-audited action router (WP-4: surface
   * cards no longer `useMutation` — every mutation is an `onAction`).
   */
  onAction: (action: string, params: Record<string, unknown>) => void;
}

const FEEDBACK_OPTIONS: { signal: FeedbackSignal; label: string }[] = [
  { signal: "marked_wrong", label: "Mark wrong" },
  { signal: "misleading", label: "Misleading" },
  { signal: "false_positive", label: "False positive" },
  { signal: "thumbs_down", label: "Just thumbs down" },
];

export function FeedbackButton({ targetKind, targetId, surface, onAction }: FeedbackButtonProps) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [submitted, setSubmitted] = useState<FeedbackSignal | null>(null);

  const submit = (signal: FeedbackSignal) => {
    onAction("feedback", {
      target_kind: targetKind,
      target_id: targetId,
      signal,
      note,
      context: surface ? { surface } : {},
    });
    setSubmitted(signal);
    setOpen(false);
    setNote("");
  };

  if (submitted) {
    return (
      <span className="inline-flex items-center bg-badge-completed-bg px-1.5 py-0.5 text-[10px] font-medium text-badge-completed-fg">
        Thanks — flagged
      </span>
    );
  }

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="p-1 text-xs text-ink-muted hover:bg-elevated hover:text-ink-soft"
        title="Flag a problem with this card"
        data-testid={`feedback-${targetKind}-${targetId}`}
      >
        👎
      </button>
      {open && (
        <div
          className="absolute right-0 z-10 mt-1 w-56 border border-line-strong bg-elevated p-2 text-xs"
          onClick={(e) => e.stopPropagation()}
        >
          <p className="mb-2 font-medium text-ink-soft">What's wrong?</p>
          <ul className="mb-2 space-y-1">
            {FEEDBACK_OPTIONS.map((opt) => (
              <li key={opt.signal}>
                <button
                  type="button"
                  onClick={() => submit(opt.signal)}
                  className="w-full px-2 py-1 text-left text-ink-soft hover:bg-elevated"
                >
                  {opt.label}
                </button>
              </li>
            ))}
          </ul>
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Optional note (e.g. 'cited chunk doesn't say this')"
            rows={2}
            className="w-full resize-none border border-line px-2 py-1 text-xs focus:border-line-strong focus:outline-none"
          />
          <div className="mt-2 flex justify-end">
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[10px] text-ink-muted hover:text-ink"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

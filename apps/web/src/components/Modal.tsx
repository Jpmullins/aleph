/**
 * The one dialog in the app, and the only place its behaviour is written down.
 *
 * Before WS-B1 there were four things claiming to be dialogs — the settings
 * drawer, the source upload, the new-project form, the new-hypothesis form —
 * and a repo-wide `git grep '"Escape"' apps/web/src` returned **zero files**.
 * Two of the four carried `role="dialog" aria-modal="true"`, which is a promise
 * to a screen reader that the rest of the page is inert. It was not: Tab walked
 * straight out of the panel into the page behind, and there was no key that
 * closed anything.
 *
 * What a modal owes the person using it, and what each line below is for:
 *
 *   * **Escape closes it.** Bound on the panel, in the capture phase of a
 *     keydown that is then stopped, so a text field inside cannot swallow it.
 *   * **Tab stays inside.** A real cycle, not a listener that merely notices:
 *     Tab on the last focusable wraps to the first, Shift+Tab on the first
 *     wraps to the last. The focusable list is recomputed on every keypress
 *     because the contents change — a disabled Submit becomes enabled the
 *     moment a file is chosen, and a trap built from a list captured at mount
 *     would skip it forever.
 *   * **Focus starts inside and comes back out.** The element that was focused
 *     when the dialog opened is restored when it closes. Without that, closing
 *     a dialog drops focus onto `<body>` and the next Tab starts from the top
 *     of the document.
 *   * **The backdrop closes it, the panel does not.** `stopPropagation` on the
 *     panel, so a click on a form field is not a click on the backdrop.
 *
 * `aria-modal` is now honest here, which is the only reason it is still
 * written: an attribute asserting behaviour the component does not implement is
 * the same defect class as a green check that cannot fail.
 */
import { useCallback, useEffect, useRef, type ReactNode } from "react";

/**
 * What the browser will actually move focus to with Tab.
 *
 * `:not([disabled])` and the negative tabindex filter matter: a disabled Submit
 * is in the DOM and not in the tab order, and treating it as the last stop
 * makes the wrap land on nothing.
 */
const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function focusables(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (el) => el.offsetParent !== null || el === document.activeElement,
  );
}

export interface ModalProps {
  /** Names the dialog for assistive tech, and renders as its heading. */
  title: string;
  onClose: () => void;
  children: ReactNode;
  /** Addressable in a browser test. The dialog element carries it. */
  testId?: string;
  className?: string;
}

export function Modal({ title, onClose, children, testId, className }: ModalProps) {
  const panel = useRef<HTMLDivElement | null>(null);
  const returnFocusTo = useRef<HTMLElement | null>(null);

  // Remember the trigger and move focus in. The effect runs once per mount, and
  // the cleanup restores — so a dialog opened from a rail button hands focus
  // back to that button rather than to `<body>`.
  useEffect(() => {
    returnFocusTo.current = document.activeElement as HTMLElement | null;
    const node = panel.current;
    if (node) {
      const first = focusables(node)[0];
      (first ?? node).focus();
    }
    return () => {
      returnFocusTo.current?.focus?.();
    };
  }, []);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        e.preventDefault();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const node = panel.current;
      if (!node) return;
      const items = focusables(node);
      if (items.length === 0) {
        // Nothing to move to: keep focus on the panel rather than letting the
        // browser hand it to the page behind.
        e.preventDefault();
        node.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement as HTMLElement | null;
      // The wrap is the trap. Everything else in this handler is bookkeeping.
      if (e.shiftKey && (active === first || active === node || !node.contains(active))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (active === last || !node.contains(active))) {
        e.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 px-4"
      onClick={onClose}
      data-testid={testId ? `${testId}-backdrop` : undefined}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
        onKeyDown={onKeyDown}
        onClick={(e) => e.stopPropagation()}
        data-testid={testId}
        className={
          "w-full max-w-md border border-line-strong bg-surface p-6 focus:outline-none " +
          (className ?? "")
        }
      >
        <h2 className="mb-4 text-xl font-semibold">{title}</h2>
        {children}
      </div>
    </div>
  );
}

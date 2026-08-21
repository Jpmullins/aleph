/**
 * The shared-context bar — what the analyst and the agent are both looking at.
 *
 * Aleph already computes this exact payload and sends it to the agent every
 * turn via `useAgentContext` in `CopilotChatSurface` (active tab, open wiki
 * page, current selection). It was never shown to the human, so "summarize this
 * page" worked by apparent magic: the analyst had no way to know what the agent
 * could see, and no way to tell when it was looking at something stale.
 *
 * Rendering the same payload closes that loop. It is read-only and derives
 * entirely from `WorkspaceUIProvider` — no fetching, no polling, no second
 * source of truth. Clicking a segment moves the workspace through the ordinary
 * setters, so the agent's next turn sees the change through the same channel it
 * always did.
 *
 * Borrowed from benchmesh's `context-bar.tsx`; the idea is theirs, the state is
 * Aleph's own.
 */
import { Icons } from "@/components/Icons";
import { usePaneKinds, useWorkspaceUI } from "@/lib/workspace-ui";

export function ContextBar({
  projectId,
  projectTitle,
  onUpload,
}: {
  projectId: string;
  projectTitle?: string;
  onUpload?: () => void;
}) {
  const paneKinds = usePaneKinds(projectId);
  const { activeSurface, setActiveSurface, openPageTitle, setOpenPageId, selection } =
    useWorkspaceUI();

  return (
    <div
      className="flex items-center gap-2 border-b border-line bg-surface px-3 py-1.5 text-xs"
      data-testid="context-bar"
      aria-label="Shared context: what you and the assistant are looking at"
    >
      <span className="font-semibold uppercase tracking-[0.09em] text-ink-muted">
        Looking at
      </span>

      {projectTitle && (
        <>
          <span
            className="max-w-[16rem] truncate font-semibold text-ink"
            data-testid="context-project"
            title={projectTitle}
          >
            {projectTitle}
          </span>
          <span aria-hidden className="text-ink-muted">
            ›
          </span>
        </>
      )}

      {/* Active surface. Cycles through the tabs so the bar is usable on its
          own, and keeps the agent's `active_tab` honest. */}
      <button
        type="button"
        data-testid="context-active-tab"
        onClick={() => {
          // Cycle the surfaces that actually exist, in the order the server
          // returned them — not a compiled-in list that may not match.
          const tabs = paneKinds.launchable.map((k) => k.title);
          if (tabs.length === 0) return;
          const i = tabs.indexOf(activeSurface);
          setActiveSurface(tabs[(i + 1) % tabs.length]);
        }}
        // Distinct from the surface tab of the same visible text. Without this
        // there are two buttons named e.g. "Wiki" that do different things —
        // ambiguous to a screen reader, and it broke an existing Playwright
        // spec under strict-mode locator resolution.
        aria-label={`Active surface: ${activeSurface}. Activate to cycle.`}
        title="Cycle the right-hand surface"
        className="inline-flex items-center gap-1.5  border border-line bg-sunken px-2 py-0.5 font-semibold text-ink hover:border-[var(--accent,#f97316)]"
      >
        {activeSurface}
      </button>

      {openPageTitle && (
        <>
          <span aria-hidden className="text-ink-muted">
            ›
          </span>
          <button
            type="button"
            data-testid="context-open-page"
            onClick={() => setOpenPageId(null)}
            title="Close the open page"
            className="max-w-[22rem] truncate  px-2 py-0.5 text-ink-soft hover:bg-sunken hover:text-ink"
          >
            {openPageTitle}
          </button>
        </>
      )}

      {/* A live selection is the least discoverable part of the shared state
          and the most surprising when the agent acts on it. */}
      {selection?.text && (
        <span
          data-testid="context-selection"
          className="max-w-[16rem] truncate  bg-[var(--accent-muted,rgba(249,115,22,0.10))] px-2 py-0.5 text-[var(--accent,#f97316)]"
          title={selection.text}
        >
          selection: “{selection.text}”
        </span>
      )}

      <span className="flex-1" />

      {/* Ingest is the one action that must be reachable from every surface —
          it used to live in the deleted LeftPanel. It sits next to the shared
          context because "what am I looking at" and "add to what I'm looking
          at" are the same thought. */}
      {onUpload && (
        <button
          type="button"
          onClick={onUpload}
          data-testid="context-upload-source"
          className="inline-flex items-center gap-1  border border-line-strong px-2 py-0.5 font-medium text-ink-soft hover:border-accent hover:text-accent"
        >
          <Icons.upload size={13} />
          Source
        </button>
      )}

      <span
        className="text-ink-muted"
        title="The assistant receives exactly this context with every message."
      >
        shared with assistant
      </span>
    </div>
  );
}

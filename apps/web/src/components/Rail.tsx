/**
 * The object rail — one entry per durable thing in the project.
 *
 * Replaces the five `flex-1` tabs that sat above the right panel. That bar had
 * a hard ceiling: each tab shares the panel width, so a sixth or seventh
 * degrades to an unreadable ~60px label. Aleph needs more surfaces than that —
 * a grounding inspector, a dispute queue, a claim view — and none of them could
 * have been added.
 *
 * A rail scales vertically, is always visible regardless of which surface is
 * open, and gives every surface a stable position the eye can learn.
 */
import { useEffect } from "react";

import { AlephLogo } from "@/components/AlephLogo";
import { Icons, type IconName } from "@/components/Icons";
import { usePaneKinds, useWorkspaceUI } from "@/lib/workspace-ui";

// No icon map here any more: the registry carries each pane's icon, so adding a
// pane is one entry rather than an entry plus a parallel table to forget.
//
// WS-B1 deleted the second list that used to sit at the bottom of this file: a
// four-tuple of `[kind, icon, label]` for settings / logs / notifications /
// profile, each opening a slide-over that covered the workspace. They are
// ordinary pane kinds now, so they come down the same `usePaneKinds` fetch as
// everything else and this component knows none of their names. The tuple was
// also the last thing in the client that decided what was openable — which is
// why `check-pane-registry.sh` could see four registry ids hardcoded here and
// `Drawers.tsx` could ship a settings screen no plugin could contribute to.

interface Props {
  projectId: string;
  onBack: () => void;
}

export function Rail({ projectId, onBack }: Props) {
  // Whatever is loaded — the rail no longer knows any surface name in advance.
  const paneKinds = usePaneKinds(projectId);
  // The rail is a *launcher*, not a switcher: clicking opens a pane beside
  // what is already there rather than replacing it. That is the whole
  // difference between tabs and a workspace you can compare things in.
  const { panes, focusedPaneId, openPane, setFocusedPaneId } = useWorkspaceUI();

  /**
   * Seed the board with the FIRST surface the server offers.
   *
   * The workspace state used to boot holding a pane called `Wiki`, which is the
   * client deciding that a research-suite surface is what a workbench opens
   * with — the one thing `GET /panes` exists to stop. Here it is whatever the
   * server put first, by id, and a project whose plugins contribute nothing
   * launchable simply opens on the Board's empty state.
   *
   * Keyed on the length rather than on the array so it does not re-run every
   * time a pane is added, and it re-seeds after the last pane is closed — the
   * "never leave an empty stage" guarantee, without a name in the client.
   */
  const first = paneKinds.launchable[0];
  const empty = panes.length === 0;
  useEffect(() => {
    if (!empty || !first) return;
    // `openPane` is deliberately not a dependency: it is re-created on every
    // render of the provider, so depending on it would re-run this effect for
    // any unrelated state change anywhere in the workspace.
    openPane(first.id, { title: first.title });
  }, [empty, first?.id, first?.title]);

  return (
    <nav
      className="flex w-14 shrink-0 flex-col items-center gap-0.5 border-r border-line bg-surface py-2.5"
      aria-label="Project surfaces"
      data-testid="rail"
    >
      <button
        type="button"
        onClick={onBack}
        aria-label="Back to projects"
        title="Projects"
        className="mb-2 grid h-9 w-9 place-items-center border border-line-strong text-accent hover:bg-sunken"
      >
        {/* The mark, not the letter. `ℵ` was a placeholder standing in for a
            logo that did not exist; it does now, and a typed glyph renders in
            whatever the system font decides rather than in our own drawing. */}
        <AlephLogo size={18} variant="mark" />
      </button>

      {paneKinds.launchable.map((kind) => {
        // The pane's wire ID, never its title. `paneKey` lower-cases what it is
        // given and uses the result as the `surfaceId` in `?panes=`, so passing
        // `kind.title` here made "Dispute Queue" into the pane `dispute queue`,
        // which the server drops as an unknown tab. Every core pane's title
        // happens to lower-case to its id, so nothing ever caught it.
        const paneId = kind.id;
        // An icon a plugin names but we do not ship must not throw.
        const Icon = Icons[(kind.icon as IconName)] ?? Icons.notes;
        const open = panes.find((p) => p.kind === paneId);
        const active = open?.id === focusedPaneId;
        return (
          <button
            key={paneId}
            type="button"
            onClick={() =>
              open ? setFocusedPaneId(open.id) : openPane(paneId, { title: kind.title })
            }
            aria-label={kind.title}
            aria-current={active ? "page" : undefined}
            data-testid={`rail-${paneId}`}
            data-active={active}
            data-open={!!open}
            className={
              "group relative grid h-10 w-10 place-items-center  " +
              (active
                ? "bg-accent-muted text-accent"
                : "text-ink-muted hover:bg-sunken hover:text-ink")
            }
          >
            {/* Active marker on the rail edge, so the current surface is
                legible from the periphery without reading the icon. */}
            {active && (
              <span
                aria-hidden
                className="absolute -left-2.5 top-2.5 bottom-2.5 w-[3px] bg-accent"
              />
            )}
            <Icon />
            <span
              aria-hidden
              className="pointer-events-none absolute left-12 z-50 whitespace-nowrap border border-line-strong bg-elevated px-2 py-1 text-xs text-ink opacity-0 transition-opacity group-hover:opacity-100"
            >
              {kind.title}
            </span>
          </button>
        );
      })}

    </nav>
  );
}

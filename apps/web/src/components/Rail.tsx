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
import { Icons, type IconName } from "@/components/Icons";
import { PANE_REGISTRY, SURFACE_TABS, useWorkspaceUI } from "@/lib/workspace-ui";

// No icon map here any more: the registry carries each pane's icon, so adding a
// pane is one entry rather than an entry plus a parallel table to forget.

interface Props {
  onBack: () => void;
  onOpenDrawer: (kind: "settings" | "logs" | "notifications" | "profile") => void;
}

export function Rail({ onBack, onOpenDrawer }: Props) {
  // The rail is a *launcher*, not a switcher: clicking opens a pane beside
  // what is already there rather than replacing it. That is the whole
  // difference between tabs and a workspace you can compare things in.
  const { panes, focusedPaneId, openPane, setFocusedPaneId } = useWorkspaceUI();

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
        className="mb-2 grid h-9 w-9 place-items-center rounded-lg border border-line-strong font-mono text-[15px] font-bold text-accent hover:bg-sunken"
      >
        ℵ
      </button>

      {SURFACE_TABS.map((tab) => {
        const Icon = Icons[(PANE_REGISTRY[tab].icon as IconName)];
        const open = panes.find((p) => p.kind === tab);
        const active = open?.id === focusedPaneId;
        return (
          <button
            key={tab}
            type="button"
            onClick={() => (open ? setFocusedPaneId(open.id) : openPane(tab))}
            aria-label={tab}
            aria-current={active ? "page" : undefined}
            data-testid={`rail-${tab.toLowerCase()}`}
            data-active={active}
            data-open={!!open}
            className={
              "group relative grid h-10 w-10 place-items-center rounded-lg " +
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
                className="absolute -left-2.5 top-2.5 bottom-2.5 w-[3px] rounded-full bg-accent"
              />
            )}
            <Icon />
            <span
              aria-hidden
              className="pointer-events-none absolute left-12 z-50 whitespace-nowrap rounded-md border border-line bg-elevated px-2 py-1 text-xs text-ink opacity-0 shadow-lg transition-opacity group-hover:opacity-100"
            >
              {tab}
            </span>
          </button>
        );
      })}

      <div className="flex-1" />

      {(
        [
          ["settings", "settings", "Settings"],
          ["logs", "ledger", "Action ledger"],
          ["notifications", "bell", "Notifications"],
          ["profile", "profile", "Profile"],
        ] as const
      ).map(([kind, icon, label]) => {
        const Icon = Icons[icon];
        return (
          <button
            key={kind}
            type="button"
            onClick={() => onOpenDrawer(kind)}
            aria-label={label}
            title={label}
            data-testid={`rail-${kind}`}
            className="grid h-9 w-9 place-items-center rounded-lg text-ink-muted hover:bg-sunken hover:text-ink"
          >
            <Icon size={17} />
          </button>
        );
      })}
    </nav>
  );
}

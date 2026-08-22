import type { RendererProps } from "./_shared";

/**
 * What the assistant did, and where it stopped.
 *
 * Until WS-C3a there was nothing to render. A chat turn wrote no `AgentRun` and
 * no events; seventeen places in the tree constructed `AgentRun` and not one of
 * them was a conversation. The only place an agent failure was legible was the
 * API container's stderr — which is what this pane exists to remove the need
 * for.
 *
 * The negative states are the point, as in `GroundingSurface`. A run with no
 * events is not "nothing here": it is a turn that died before its first tool
 * call, which is the most informative shape a reader can be shown. A failed run
 * leads with its error rather than burying it under a timeline.
 *
 * Data-bound only: every value arrives in `props` as an `updateDataModel`
 * delta. A pane owns no transport, so what is drawn is exactly what the server
 * resolved — no fetching, no client-side reconciliation of two sources.
 *
 * Squared and token-only by construction, not by later cleanup:
 * `--radius-none: 0px` is the stated design and the existing 180-violation
 * backlog is what happens when new surfaces are written first and conformed
 * afterwards. No `rounded-*`, no `shadow-*`, no palette-scale colour.
 */

interface RunRow {
  id: string;
  status: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  error_text?: string | null;
}

interface EventRow {
  kind: string;
  tool?: string | null;
  subagent?: string | null;
  tool_call_id?: string | null;
  duration_ms?: number | null;
  args?: Record<string, unknown> | null;
  error_class?: string | null;
  error?: string | null;
  at?: string | null;
}

interface InspectorProps {
  runs?: RunRow[];
  selected?: RunRow | null;
  events?: EventRow[];
}

const TERMINAL_FAILURE = "failed";

function clock(iso?: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString();
}

function millis(ms?: number | null): string {
  if (ms === null || ms === undefined) return "";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

/** Tool calls, paired by `tool_call_id`, so one row is one call. */
function pairEvents(events: EventRow[]): EventRow[] {
  const byCall = new Map<string, EventRow>();
  const ordered: EventRow[] = [];
  for (const e of events) {
    const key = e.tool_call_id;
    if (!key) {
      ordered.push(e);
      continue;
    }
    const existing = byCall.get(key);
    if (!existing) {
      byCall.set(key, { ...e });
      ordered.push(byCall.get(key) as EventRow);
      continue;
    }
    // A finish or a failure completes the row the start opened. Merging rather
    // than appending is what makes the timeline read as calls instead of as a
    // log with every entry twice.
    Object.assign(existing, {
      ...e,
      kind: e.kind,
      args: existing.args ?? e.args,
      tool: existing.tool ?? e.tool,
    });
  }
  return ordered;
}

export function InspectorSurface({ component }: RendererProps) {
  // `component.props`, matching every other surface in this directory. The
  // renderer contract passes the whole component, not a destructured props bag.
  const props = (component.props ?? {}) as InspectorProps;
  const runs: RunRow[] = props.runs ?? [];
  const selected = props.selected ?? null;
  const events = pairEvents(props.events ?? []);

  if (runs.length === 0) {
    return (
      <div className="flex h-full flex-col p-3" data-testid="inspector-surface">
        <div className="mb-2 text-xs uppercase tracking-wider text-ink-muted">Inspector</div>
        <p className="text-sm text-ink-muted">
          No assistant runs on this project yet. A run appears here the moment a chat turn
          starts — including one that fails before it answers.
        </p>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col p-3" data-testid="inspector-surface">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="text-xs uppercase tracking-wider text-ink-muted">Inspector</div>
        <div className="text-xs text-ink-muted">{runs.length} run(s)</div>
      </div>

      <div className="flex min-h-0 flex-1 gap-3">
        <ol className="w-56 shrink-0 overflow-y-auto border border-line" data-testid="run-list">
          {runs.map((run: RunRow) => {
            const active = selected?.id === run.id;
            return (
              <li
                key={run.id}
                data-testid="run-row"
                data-active={active ? "true" : undefined}
                className={
                  "border-b border-line px-2 py-1.5 text-xs last:border-b-0 " +
                  (active ? "bg-sunken text-ink" : "text-ink-muted")
                }
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono">{run.id.slice(0, 8)}</span>
                  <StatusTag status={run.status} />
                </div>
                <div className="mt-0.5 flex items-center justify-between gap-2">
                  <span>{clock(run.started_at)}</span>
                  <span>{millis(run.duration_ms)}</span>
                </div>
              </li>
            );
          })}
        </ol>

        <div className="min-w-0 flex-1 overflow-y-auto">
          {selected === null ? (
            <p className="text-sm text-ink-muted">Select a run.</p>
          ) : (
            <>
              <div className="mb-2 border border-line bg-elevated p-2.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-xs text-ink">{selected.id}</span>
                  <StatusTag status={selected.status} />
                </div>
                <div className="mt-1 text-xs text-ink-muted">
                  started {clock(selected.started_at)} · finished {clock(selected.completed_at)}
                  {selected.duration_ms != null ? ` · ${millis(selected.duration_ms)}` : ""}
                </div>
                {selected.error_text ? (
                  // The failure leads. A reader opening a failed run is looking
                  // for this and nothing else.
                  <p
                    className="mt-2 border border-line-strong bg-sunken p-2 font-mono text-xs text-ink"
                    data-testid="run-error"
                  >
                    {selected.error_text}
                  </p>
                ) : null}
              </div>

              {events.length === 0 ? (
                <p className="text-sm text-ink-muted" data-testid="empty-timeline">
                  This run recorded no tool calls
                  {selected.status === TERMINAL_FAILURE
                    ? " — it failed before making one."
                    : "."}
                </p>
              ) : (
                <ol data-testid="run-timeline">
                  {events.map((event, index) => (
                    <ToolCallRow key={`${event.tool_call_id ?? "e"}-${index}`} event={event} />
                  ))}
                </ol>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function StatusTag({ status }: { status: string }) {
  const failed = status === TERMINAL_FAILURE;
  return (
    <span
      data-testid="run-status"
      className={
        "px-1.5 py-0.5 text-[10px] uppercase tracking-wide " +
        (failed ? "bg-ink text-ink-inverse" : "bg-sunken text-ink-muted")
      }
    >
      {status}
    </span>
  );
}

function ToolCallRow({ event }: { event: EventRow }) {
  const failed = event.kind === "tool_failed";
  const args = event.args ? Object.entries(event.args) : [];
  return (
    <li className="border-b border-line py-1.5 last:border-b-0" data-testid="tool-call">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs text-ink">{event.tool ?? event.kind}</span>
        <span className="text-[10px] text-ink-muted">
          {event.subagent ?? "orchestrator"}
          {event.duration_ms != null ? ` · ${millis(event.duration_ms)}` : ""}
        </span>
      </div>
      {args.length > 0 ? (
        <div className="mt-0.5 font-mono text-[10px] text-ink-muted" data-testid="tool-args">
          {args.map(([k, v]) => `${k}=${String(v)}`).join("  ")}
        </div>
      ) : null}
      {failed ? (
        <p
          className="mt-1 border border-line-strong bg-sunken p-1.5 font-mono text-[10px] text-ink"
          data-testid="tool-error"
        >
          {event.error_class ? `${event.error_class}: ` : ""}
          {event.error ?? "the tool failed and recorded no message"}
        </p>
      ) : null}
    </li>
  );
}

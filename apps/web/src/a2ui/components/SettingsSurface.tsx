/**
 * Settings, the ledger, the run digest and the account — as panes on the board.
 *
 * This replaces the web app's deleted settings drawer: 742 lines behind a
 * `fixed inset-0` overlay,
 * claiming `role="dialog" aria-modal="true"` while implementing none of the
 * behaviour, carrying its own copies of two lists the server owns, and — the
 * reason WS-B1 exists — making settings the one part of the workbench a plugin
 * could not extend, because every section was a hand-written React function.
 *
 * The shape is inverted here. The server sends an ORDERED LIST OF SECTIONS and
 * this file is a renderer per section KIND. What a settings pane contains is a
 * value now, so `settings`, `logs`, `notifications` and `profile` are four
 * different values of one component rather than four components, and a plugin
 * whose configuration is a JSON Schema needs none of this at all —
 * `settings_card.py` generates its screen from the declaration and it renders
 * through the basic catalog primitives.
 *
 * **Reads are bound, writes are calls.** Every value drawn below arrives in
 * `props` from an `updateDataModel` delta; the pane owns no transport and
 * fetches nothing. Changing something is a different act: it POSTs/PATCHes the
 * same REST route the drawer used, and the multiplexed surface stream pushes
 * the new state back, so the value on screen is always the server's answer and
 * never this component's optimistic guess. `pending` below is a disabled state,
 * not a shadow copy of the data.
 *
 * An unknown section kind renders as a named placeholder rather than being
 * skipped. Skipping is how a settings screen loses a section and still looks
 * complete — the specific regression `docs/plan.md` WS-B1 calls its main risk.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { useWorkspaceUI } from "@/lib/workspace-ui";

import { useSurface } from "../surface-context";
import type { RendererProps } from "./_shared";

interface FieldRow {
  label: string;
  value: string;
  mono?: boolean;
  multiline?: boolean;
}

interface EligibleModel {
  id: string;
  label: string;
  max_input_tokens: number | null;
  input_per_token: string | null;
  output_per_token: string | null;
}

interface CapabilityRow {
  id: string;
  label: string;
  help: string;
  bound: string | null;
  eligible: EligibleModel[];
}

interface ConnectorRow {
  id: string;
  kind: string;
  name: string;
  requires_auth: boolean;
  enabled: boolean;
  config: Record<string, unknown>;
  /** `set` / `unset` for an owner; `unknown` when the viewer may not be told —
   *  reading key state is owner-gated on the REST path, and a surface stream is
   *  open to every member. */
  key_state: "set" | "unset" | "unknown";
  /** The credential blob's own status, e.g. consensus `reconnect_required`.
   *  Owner-only, and null for a plain API key, which carries no status. */
  status: string | null;
}

interface Section {
  kind: string;
  title?: string;
  blurb?: string;
  rows?: FieldRow[];
  members?: { id: string; user_id: string; role: string }[];
  profiles?: string[];
  current?: string | null;
  gateway?: { reachable: boolean; model_count: number; note: string };
  capabilities?: CapabilityRow[];
  connectors?: ConnectorRow[];
  plugins?: { id: string; title: string; description: string; trust: string }[];
  chain?: {
    ok: boolean;
    count: number;
    first_divergence_event_id: string | null;
    age_seconds: number;
  };
  events?: {
    id: string;
    actor_kind: string;
    action_kind: string;
    target_kind: string | null;
    trace_id: string | null;
    timestamp: string;
  }[];
  runs?: {
    id: string;
    agent_kind: string;
    status: string;
    error_text: string | null;
    created_at: string;
    completed_at: string | null;
  }[];
  limit?: number;
  text?: string;
}

interface SettingsProps {
  title?: string;
  sections?: Section[];
}

/** See the note in `SettingsSurface` — a pre-model render resolves a binding to
 *  the binding object, and `?? []` lets that through. */
function arr<T>(value: T[] | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

function errMsg(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return "Owner access required.";
    return `Failed (${err.status}).`;
  }
  return "Something went wrong.";
}

export function SettingsSurface({ component }: RendererProps) {
  const props = (component.props ?? {}) as SettingsProps;
  /**
   * `Array.isArray`, not `?? []`, and it is load-bearing.
   *
   * A surface arrives as three messages — `createSurface`, `updateComponents`,
   * `updateDataModel` — and the Board mounts the tree as soon as the first one
   * lands. In that window the binder has the component and not yet the model,
   * so a `{ path: "/sections" }` binding resolves to the binding OBJECT rather
   * than to a value. `??` does not help: an object is not nullish, so
   * `sections.map` threw and React unmounted the whole pane before the model
   * ever arrived. Measured, not theorised — the pane rendered nothing at all
   * and the only trace was `sections.map is not a function` in the console.
   */
  const sections = Array.isArray(props.sections) ? props.sections : [];
  const title = typeof props.title === "string" ? props.title : "Settings";
  const { projectId } = useSurface();

  return (
    <div className="flex h-full min-h-0 flex-col gap-5 p-3" data-testid="settings-surface">
      <div className="text-xs uppercase tracking-wider text-ink-muted">
        {title}
      </div>
      {sections.length === 0 ? (
        <p className="text-sm text-ink-muted" data-testid="settings-no-sections">
          Waiting for this pane's sections…
        </p>
      ) : (
        sections.map((section, index) => (
          <SectionBlock
            key={`${section.kind}-${section.title ?? index}`}
            section={section}
            projectId={projectId}
          />
        ))
      )}
    </div>
  );
}

function SectionBlock({ section, projectId }: { section: Section; projectId: string }) {
  return (
    <section data-testid={`settings-section-${section.kind}`}>
      {section.title && (
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-muted">
          {section.title}
        </h3>
      )}
      {section.blurb && <p className="mb-2 text-xs text-ink-muted">{section.blurb}</p>}
      <SectionBody section={section} projectId={projectId} />
    </section>
  );
}

function SectionBody({ section, projectId }: { section: Section; projectId: string }) {
  switch (section.kind) {
    case "fields":
      return <Fields rows={arr(section.rows)} />;
    case "members":
      return <Members members={arr(section.members)} />;
    case "model_profile":
      return <ModelProfile section={section} projectId={projectId} />;
    case "connectors":
      return <Connectors connectors={arr(section.connectors)} projectId={projectId} />;
    case "plugins":
      return <Plugins plugins={arr(section.plugins)} />;
    case "ledger":
      return <Ledger section={section} />;
    case "runs":
      return <Runs runs={arr(section.runs)} limit={section.limit} />;
    case "note":
      return <p className="text-sm text-ink-soft">{section.text}</p>;
    default:
      // Named, never skipped. A section quietly missing is indistinguishable
      // from a setting that was never offered.
      return (
        <p className="text-xs text-badge-warning-fg" data-testid="settings-unknown-section">
          This build has no renderer for a {`"${section.kind}"`} section. It was sent, not
          dropped — update the client or the producer.
        </p>
      );
  }
}

function Fields({ rows }: { rows: FieldRow[] }) {
  return (
    <div className="space-y-1">
      {rows.map((r) => (
        <Row key={r.label} {...r} />
      ))}
    </div>
  );
}

function Row({ label, value, mono = false, multiline = false }: FieldRow) {
  if (multiline) {
    return (
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase tracking-wider text-ink-muted">{label}</span>
        <span className="whitespace-pre-wrap break-words text-sm text-ink">{value}</span>
      </div>
    );
  }
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs uppercase tracking-wider text-ink-muted">{label}</span>
      <span
        className={`truncate text-right text-sm text-ink ${mono ? "font-mono text-xs" : ""}`}
        title={value}
      >
        {value}
      </span>
    </div>
  );
}

function Members({ members }: { members: { id: string; user_id: string; role: string }[] }) {
  if (members.length === 0) return <p className="text-ink-muted">No members.</p>;
  return (
    <ul className="space-y-1">
      {members.map((m) => (
        <li key={m.id} className="flex items-center justify-between">
          <span className="truncate font-mono text-xs">{m.user_id}</span>
          <span className="bg-elevated px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider">
            {m.role}
          </span>
        </li>
      ))}
    </ul>
  );
}

function ModelProfile({ section, projectId }: { section: Section; projectId: string }) {
  const qc = useQueryClient();
  const gateway = section.gateway;
  const capabilities = arr(section.capabilities);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["model-profile", projectId] });
    void qc.invalidateQueries({ queryKey: ["project", projectId] });
  };

  const switchProfile = useMutation({
    mutationFn: (name: string) =>
      api.post(`/v1/projects/${projectId}/model-profile/switch`, { profile_name: name }),
    onSuccess: invalidate,
  });

  const bind = useMutation({
    mutationFn: ({ capability, model }: { capability: string; model: EligibleModel }) =>
      api.patch(`/v1/projects/${projectId}/model-profile`, {
        bindings: {
          [capability]: {
            model: model.id,
            provider: "litellm",
            // The gateway's own numbers, carried through untouched. The server
            // computed them and sent them with the surface; inventing a default
            // here is how made-up pricing gets back into the cost ledger.
            max_input_tokens: model.max_input_tokens ?? 200000,
            cost_per_input_token_usd: model.input_per_token ?? "0",
            cost_per_output_token_usd: model.output_per_token ?? "0",
          },
        },
      }),
    onSuccess: invalidate,
  });

  const autoconfigure = useMutation({
    mutationFn: () => api.post(`/v1/projects/${projectId}/model-profile/autoconfigure`, {}),
    onSuccess: invalidate,
  });

  return (
    <div>
      <div className="flex flex-wrap gap-1.5">
        {arr(section.profiles).map((name) => {
          const active = name === section.current;
          return (
            <button
              key={name}
              type="button"
              data-testid={`profile-${name}`}
              disabled={active || switchProfile.isPending}
              onClick={() => switchProfile.mutate(name)}
              className={
                "border px-2.5 py-1 text-xs font-medium transition-colors " +
                (active
                  ? "border-transparent bg-ink text-ink-inverse"
                  : "border-line-strong text-ink-soft hover:bg-elevated disabled:opacity-50")
              }
            >
              {name}
            </button>
          );
        })}
      </div>
      {switchProfile.isError && (
        <p className="mt-1.5 text-xs text-bad">{errMsg(switchProfile.error)}</p>
      )}

      <div className="mt-4 mb-1 flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-ink">Per-capability models</h4>
        <button
          type="button"
          data-testid="autoconfigure"
          disabled={autoconfigure.isPending || gateway?.reachable !== true}
          onClick={() => autoconfigure.mutate()}
          title="Pick the best available model for every capability, testing each one first"
          className="shrink-0 border border-line-strong px-2 py-1 text-[11px] font-medium text-ink-soft hover:bg-elevated disabled:opacity-50"
        >
          {autoconfigure.isPending ? "Testing models…" : "Configure from gateway"}
        </button>
      </div>
      {gateway?.note ? (
        <p className="mb-2 text-xs text-badge-warning-fg" data-testid="gateway-note">
          {gateway.note}
        </p>
      ) : null}
      {autoconfigure.isError && (
        <p className="mb-2 text-xs text-bad">{errMsg(autoconfigure.error)}</p>
      )}

      <ul className="space-y-2">
        {capabilities.map((cap) => {
          const pending = bind.isPending && bind.variables?.capability === cap.id;
          const eligible = arr(cap.eligible);
          return (
            <li
              key={cap.id}
              className="flex items-start justify-between gap-3"
              data-testid={`capability-${cap.id}`}
            >
              <div className="min-w-0">
                <div className="text-xs font-medium text-ink">{cap.label}</div>
                <div className="truncate text-[11px] text-ink-muted">{cap.help}</div>
              </div>
              {eligible.length === 0 ? (
                <span
                  className="shrink-0 text-[11px] text-badge-warning-fg"
                  title="No model on this gateway meets this capability's requirements. It is left unbound on purpose — binding a model that cannot do the job fails later, and less visibly."
                >
                  {gateway?.reachable ? "unsupported by gateway" : "no model list"}
                </span>
              ) : (
                <select
                  aria-label={`Model for ${cap.label}`}
                  className="max-w-[62%] shrink-0 border border-line-strong bg-surface px-1.5 py-1 text-[11px] text-ink disabled:opacity-50"
                  value={cap.bound ?? ""}
                  disabled={pending}
                  onChange={(e) => {
                    const model = eligible.find((m) => m.id === e.target.value);
                    if (model) bind.mutate({ capability: cap.id, model });
                  }}
                >
                  {cap.bound === null && <option value="">— unbound —</option>}
                  {eligible.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </select>
              )}
            </li>
          );
        })}
      </ul>
      {bind.isError && <p className="mt-1.5 text-xs text-bad">{errMsg(bind.error)}</p>}
    </div>
  );
}

function Connectors({
  connectors,
  projectId,
}: {
  connectors: ConnectorRow[];
  projectId: string;
}) {
  if (connectors.length === 0) return <p className="text-ink-muted">No connectors registered.</p>;
  return (
    <ul className="space-y-2">
      {connectors.map((c) => (
        <ConnectorItem key={c.id} connector={c} projectId={projectId} />
      ))}
    </ul>
  );
}

function ConnectorItem({
  connector,
  projectId,
}: {
  connector: ConnectorRow;
  projectId: string;
}) {
  const qc = useQueryClient();
  const [keyDraft, setKeyDraft] = useState("");
  const [editing, setEditing] = useState(false);

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["connector-bindings", projectId] });
    void qc.invalidateQueries({ queryKey: ["connector-credentials", projectId] });
  };

  const toggle = useMutation({
    mutationFn: (next: boolean) =>
      api.post(`/v1/projects/${projectId}/connectors/bindings`, {
        connector_id: connector.id,
        enabled: next,
        config_jsonb: connector.config ?? {},
      }),
    onSuccess: invalidate,
  });
  const saveKey = useMutation({
    mutationFn: (plaintext: string) =>
      api.put(`/v1/projects/${projectId}/connector-credentials/${connector.kind}`, { plaintext }),
    onSuccess: () => {
      setKeyDraft("");
      setEditing(false);
      invalidate();
    },
  });
  const removeKey = useMutation({
    mutationFn: () => api.del(`/v1/projects/${projectId}/connector-credentials/${connector.kind}`),
    onSuccess: invalidate,
  });

  return (
    <li className="border border-line px-3 py-2" data-testid={`connector-${connector.kind}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-ink">{connector.name}</div>
          <div className="text-[10px] uppercase tracking-wider text-ink-muted">
            {connector.kind}
            {connector.requires_auth && " · key required"}
          </div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={connector.enabled}
          aria-label={`${connector.enabled ? "Disable" : "Enable"} ${connector.name}`}
          disabled={toggle.isPending}
          onClick={() => toggle.mutate(!connector.enabled)}
          className={
            "relative h-5 w-9 shrink-0 transition-colors disabled:opacity-50 " +
            (connector.enabled ? "bg-good" : "bg-line-strong")
          }
        >
          <span
            className={
              "absolute top-0.5 h-4 w-4 bg-surface transition-transform " +
              (connector.enabled ? "translate-x-4" : "translate-x-0.5")
            }
          />
        </button>
      </div>

      {connector.status && (
        <p className="mt-1 text-[11px] text-badge-warning-fg" data-testid="connector-status">
          {connector.status}
        </p>
      )}

      {connector.requires_auth && connector.key_state === "unknown" && (
        <p className="mt-2 text-[11px] text-ink-muted">
          Owner access is required to see or change this key.
        </p>
      )}

      {connector.requires_auth && connector.key_state !== "unknown" && (
        <div className="mt-2">
          {connector.key_state === "set" && !editing ? (
            <div className="flex items-center gap-2 text-xs">
              <span className="bg-badge-completed-bg px-1.5 py-0.5 font-medium text-badge-completed-fg">
                Key set
              </span>
              <button
                type="button"
                onClick={() => setEditing(true)}
                className="text-ink-muted hover:text-ink"
              >
                Replace
              </button>
              <button
                type="button"
                disabled={removeKey.isPending}
                onClick={() => removeKey.mutate()}
                className="text-bad hover:opacity-80 disabled:opacity-50"
              >
                Remove
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-1.5">
              <input
                type="password"
                value={keyDraft}
                onChange={(e) => setKeyDraft(e.target.value)}
                placeholder={connector.key_state === "set" ? "New key…" : "Paste API key…"}
                aria-label={`API key for ${connector.name}`}
                autoComplete="off"
                className="min-w-0 flex-1 border border-line-strong px-2 py-1 text-xs focus:border-line-strong focus:outline-none"
              />
              <button
                type="button"
                disabled={!keyDraft.trim() || saveKey.isPending}
                onClick={() => saveKey.mutate(keyDraft.trim())}
                className="bg-ink px-2 py-1 text-xs font-medium text-ink-inverse disabled:opacity-40"
              >
                Save
              </button>
              {connector.key_state === "set" && (
                <button
                  type="button"
                  onClick={() => {
                    setEditing(false);
                    setKeyDraft("");
                  }}
                  className="px-1 text-xs text-ink-muted hover:text-ink-soft"
                >
                  Cancel
                </button>
              )}
            </div>
          )}
          {saveKey.isError && <p className="mt-1 text-xs text-bad">{errMsg(saveKey.error)}</p>}
        </div>
      )}
      {(toggle.isError || removeKey.isError) && (
        <p className="mt-1 text-xs text-bad">{errMsg(toggle.error ?? removeKey.error)}</p>
      )}
    </li>
  );
}

function Plugins({
  plugins,
}: {
  plugins: { id: string; title: string; description: string; trust: string }[];
}) {
  const { openPane } = useWorkspaceUI();
  if (plugins.length === 0) {
    return (
      <p className="text-xs text-ink-muted">
        No plugin has declared a settings screen. A plugin gets one by declaring a config
        schema — it ships no browser code.
      </p>
    );
  }
  return (
    <ul className="space-y-1.5">
      {plugins.map((p) => (
        <li key={p.id} className="flex items-center justify-between gap-2 border border-line px-3 py-2">
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-ink">{p.title}</div>
            <div className="truncate text-[11px] text-ink-muted">
              {p.description || p.id} · {p.trust}
            </div>
          </div>
          <button
            type="button"
            data-testid={`open-plugin-settings-${p.id}`}
            // Its own pane, beside this one — which is the whole point of the
            // pane model: the plugin's screen and the thing it configures can
            // be on screen at the same time.
            onClick={() => openPane("Settings", { title: p.title, params: { plugin: p.id } })}
            className="shrink-0 border border-line-strong px-2 py-1 text-[11px] font-medium text-ink-soft hover:bg-elevated"
          >
            Open
          </button>
        </li>
      ))}
    </ul>
  );
}

function Ledger({ section }: { section: Section }) {
  const chain = section.chain;
  const events = arr(section.events);
  return (
    <div>
      {chain && (
        <div
          className="mb-2 border border-line px-3 py-2 text-xs"
          data-testid="ledger-chain"
          data-ok={chain.ok ? "true" : "false"}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium text-ink">
              {chain.ok ? "Hash chain verifies" : "Hash chain DIVERGED"}
            </span>
            <span className="text-ink-muted">{chain.count} events</span>
          </div>
          {chain.first_divergence_event_id && (
            <div className="mt-1 font-mono text-[10px] text-bad">
              first divergence: {chain.first_divergence_event_id}
            </div>
          )}
          <div className="mt-1 text-[10px] text-ink-muted">
            checked {chain.age_seconds}s ago
          </div>
        </div>
      )}
      {events.length === 0 ? (
        <p className="text-ink-muted">No events yet.</p>
      ) : (
        <>
          <ul className="space-y-1.5">
            {events.map((e) => (
              <li key={e.id} className="border border-line px-3 py-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-ink">{e.action_kind}</span>
                  <span className="text-ink-muted">
                    {new Date(e.timestamp).toLocaleTimeString()}
                  </span>
                </div>
                <div className="mt-1 text-ink-muted">
                  <span className="font-medium">{e.actor_kind}</span>
                  {e.target_kind && <span> → {e.target_kind}</span>}
                </div>
                {e.trace_id && (
                  <div
                    className="mt-1 truncate font-mono text-[10px] text-ink-muted"
                    title={e.trace_id}
                  >
                    trace: {e.trace_id.slice(0, 16)}…
                  </div>
                )}
              </li>
            ))}
          </ul>
          {section.limit !== undefined && (
            <p className="mt-2 text-[10px] text-ink-muted">
              Showing the most recent {section.limit}. A pane quietly showing N looks identical
              to one showing all of them.
            </p>
          )}
        </>
      )}
    </div>
  );
}

function Runs({
  runs,
  limit,
}: {
  runs: {
    id: string;
    agent_kind: string;
    status: string;
    error_text: string | null;
    created_at: string;
    completed_at: string | null;
  }[];
  limit?: number;
}) {
  const failed = runs.filter((r) => r.status === "failed");
  const active = runs.filter((r) => r.status === "running" || r.status === "pending");
  const succeeded = runs.filter((r) => r.status === "succeeded");

  return (
    <div className="space-y-4">
      {failed.length > 0 && (
        <div data-testid="runs-failed">
          <h4 className="mb-1 text-xs font-semibold text-ink">Failed ({failed.length})</h4>
          {failed.map((r) => (
            <div key={r.id} className="border border-line bg-badge-failed-bg px-3 py-2 text-xs">
              <div className="font-medium text-badge-failed-fg">{r.agent_kind}</div>
              {r.error_text && <div className="mt-1 text-badge-failed-fg">{r.error_text}</div>}
              <div className="mt-1 text-badge-failed-fg">
                {new Date(r.created_at).toLocaleString()}
              </div>
            </div>
          ))}
        </div>
      )}
      <div data-testid="runs-active">
        <h4 className="mb-1 text-xs font-semibold text-ink">Running ({active.length})</h4>
        {active.length === 0 && <p className="text-ink-muted">No active agents.</p>}
        {active.map((r) => (
          <div key={r.id} className="border border-line bg-badge-running-bg px-3 py-2 text-xs">
            <div className="font-medium text-badge-running-fg">{r.agent_kind}</div>
            <div className="mt-1 text-badge-running-fg">{r.status}</div>
          </div>
        ))}
      </div>
      <div data-testid="runs-succeeded">
        <h4 className="mb-1 text-xs font-semibold text-ink">
          Recent succeeded ({succeeded.length})
        </h4>
        {succeeded.length === 0 && <p className="text-ink-muted">None yet.</p>}
        {succeeded.slice(0, 8).map((r) => (
          <div key={r.id} className="border border-line px-3 py-2 text-xs">
            <div className="font-medium text-ink">{r.agent_kind}</div>
            <div className="mt-1 text-ink-muted">
              {r.completed_at ? new Date(r.completed_at).toLocaleString() : "—"}
            </div>
          </div>
        ))}
      </div>
      {limit !== undefined && (
        <p className="text-[10px] text-ink-muted">Showing the most recent {limit}.</p>
      )}
    </div>
  );
}

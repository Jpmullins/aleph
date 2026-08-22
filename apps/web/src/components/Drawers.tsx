import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  api,
  ApiError,
  type AutoconfigureOut,
  type ConnectorBindingOut,
  type ConnectorOut,
  type CostRollupOut,
  type CredentialOut,
  type GatewayModelOut,
  type MeOut,
  type ModelProfileOut,
  type ProjectOut,
} from "@/lib/api";
import type { DrawerKind } from "@/components/LeftPanel";

interface Props {
  kind: DrawerKind;
  projectId: string;
  onClose: () => void;
}

export function Drawer({ kind, projectId, onClose }: Props) {
  return (
    <div
      className="fixed inset-0 z-30 flex"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div className="flex-1 bg-ink/30" />
      <div
        className="flex h-full w-[28rem] flex-col border-l border-line-strong bg-surface"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-ink-soft">
            {TITLES[kind]}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="px-2 py-1 text-ink-muted hover:bg-elevated"
            aria-label="Close"
          >
            ✕
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4 text-sm text-ink-soft">
          {kind === "settings" && <SettingsBody projectId={projectId} />}
          {kind === "logs" && <LogsBody projectId={projectId} />}
          {kind === "notifications" && <NotificationsBody projectId={projectId} />}
          {kind === "profile" && <ProfileBody projectId={projectId} />}
        </div>
      </div>
    </div>
  );
}

const TITLES: Record<DrawerKind, string> = {
  settings: "Settings",
  logs: "Action ledger",
  notifications: "Notifications",
  profile: "Profile",
};

function SettingsBody({ projectId }: { projectId: string }) {
  const project = useQuery<ProjectOut>({
    queryKey: ["project", projectId],
    queryFn: () => api.get<ProjectOut>(`/v1/projects/${projectId}`),
  });
  const cost = useQuery<CostRollupOut>({
    queryKey: ["cost", projectId],
    queryFn: () => api.get<CostRollupOut>(`/v1/projects/${projectId}/cost`),
  });
  const members = useQuery<{ id: string; user_id: string; role: string }[]>({
    queryKey: ["members", projectId],
    queryFn: () => api.get(`/v1/projects/${projectId}/members`),
  });
  if (!project.data) return <p className="text-ink-muted">Loading…</p>;
  const p = project.data;
  return (
    <div className="space-y-5">
      <Section title="Project">
        <Row label="Title" value={p.title} />
        <Row label="Description" value={p.description || "—"} multiline />
        <Row label="Status" value={p.status} />
        <Row label="Created" value={new Date(p.created_at).toLocaleString()} />
      </Section>
      <Section title="Cost">
        {cost.data ? (
          <Row label="Spent (USD)" value={`$${Number(cost.data.total_usd).toFixed(4)}`} />
        ) : (
          <p className="text-ink-muted">Loading cost…</p>
        )}
      </Section>
      <Section title="Members">
        {members.data?.length === 0 && <p className="text-ink-muted">No members.</p>}
        <ul className="space-y-1">
          {members.data?.map((m) => (
            <li key={m.id} className="flex items-center justify-between">
              <span className="truncate font-mono text-xs">{m.user_id}</span>
              <span className="bg-elevated px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider">
                {m.role}
              </span>
            </li>
          ))}
        </ul>
      </Section>
      <ModelProfileSection projectId={projectId} />
      <ConnectorsSection projectId={projectId} />
    </div>
  );
}

const PROFILE_NAMES = ["aleph-dev", "aleph-production"] as const;

function ModelProfileSection({ projectId }: { projectId: string }) {
  const qc = useQueryClient();
  const current = useQuery<ModelProfileOut>({
    queryKey: ["model-profile", projectId],
    queryFn: () => api.get<ModelProfileOut>(`/v1/projects/${projectId}/model-profile`),
  });
  const templates = useQuery<ModelProfileOut[]>({
    queryKey: ["model-profile-templates"],
    queryFn: () => api.get<ModelProfileOut[]>(`/v1/model-profile-templates`),
  });
  const switchProfile = useMutation({
    mutationFn: (name: string) =>
      api.post<ModelProfileOut>(`/v1/projects/${projectId}/model-profile/switch`, {
        profile_name: name,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["model-profile", projectId] });
      void qc.invalidateQueries({ queryKey: ["project", projectId] });
    },
  });

  const names = templates.data?.map((t) => t.name) ?? [...PROFILE_NAMES];
  const currentName = current.data?.name;
  return (
    <Section title="Model profile">
      <p className="mb-2 text-xs text-ink-muted">
        The template that maps each capability (synthesis, extraction, embedding…) to a model.
        Switching re-embeds sources in the background if the embedding model changes.
      </p>
      <div className="flex flex-wrap gap-1.5">
        {names.map((name) => {
          const active = name === currentName;
          return (
            <button
              key={name}
              type="button"
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
      {switchProfile.isPending && <p className="mt-1.5 text-xs text-ink-muted">Switching…</p>}
      <CapabilityBindings projectId={projectId} profile={current.data} />
    </Section>
  );
}

/** Capability order mirrors the server enum so the list reads predictably. */
const CAPABILITIES = [
  "synthesis",
  "judge",
  "page_selection",
  "extraction",
  "classification",
  "vision",
  "code",
  "embedding",
] as const;

const CAPABILITY_HELP: Record<string, string> = {
  synthesis: "Composes briefs and wiki pages",
  judge: "Scores eval outputs",
  page_selection: "Picks wiki pages to answer from — needs a large context window",
  extraction: "Pulls claims and citations out of sources",
  classification: "Cheap routing and labelling",
  vision: "Reads figures and scanned pages",
  code: "Writes the sandboxed analysis code",
  embedding: "Vectorises chunks for intra-source descent",
};

/** "$5.50 / $27.50 per Mtok", or a plain marker when the gateway gives no price. */
function priceLabel(m: GatewayModelOut): string {
  if (!m.is_priced) return "unpriced";
  const perM = (v: string | null) => (v === null ? "—" : `$${(Number(v) * 1e6).toFixed(2)}`);
  return `${perM(m.input_per_token)} / ${perM(m.output_per_token)} per Mtok`;
}

function CapabilityBindings({
  projectId,
  profile,
}: {
  projectId: string;
  profile: ModelProfileOut | undefined;
}) {
  const qc = useQueryClient();
  const models = useQuery<GatewayModelOut[]>({
    queryKey: ["gateway-models"],
    queryFn: () => api.get<GatewayModelOut[]>(`/v1/gateway/models`),
    staleTime: 5 * 60 * 1000,
  });
  const bind = useMutation({
    mutationFn: ({ capability, model }: { capability: string; model: GatewayModelOut }) =>
      api.patch<ModelProfileOut>(`/v1/projects/${projectId}/model-profile`, {
        bindings: {
          [capability]: {
            model: model.id,
            provider: "litellm",
            // Carry the gateway's own numbers onto the binding. Omitting them
            // would fall back to the schema defaults (200k context, zero cost)
            // and quietly reintroduce made-up pricing.
            max_input_tokens: model.max_input_tokens ?? 200000,
            cost_per_input_token_usd: model.input_per_token ?? "0",
            cost_per_output_token_usd: model.output_per_token ?? "0",
          },
        },
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["model-profile", projectId] });
    },
  });
  const autoconfigure = useMutation({
    mutationFn: () =>
      api.post<AutoconfigureOut>(
        `/v1/projects/${projectId}/model-profile/autoconfigure`,
        {},
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["model-profile", projectId] });
      void qc.invalidateQueries({ queryKey: ["gateway-models"] });
    },
  });

  if (models.isLoading) {
    return <p className="mt-3 text-xs text-ink-muted">Asking the gateway which models it serves…</p>;
  }
  if (models.isError) {
    return (
      <p className="mt-3 text-xs text-bad">
        Could not reach the model gateway: {errMsg(models.error)}. Aleph ships no built-in model
        list, so there is nothing to choose from until it responds.
      </p>
    );
  }
  const available = models.data ?? [];
  if (available.length === 0) {
    return (
      <p className="mt-3 text-xs text-badge-warning-fg">
        The gateway responded but advertises no models. Check its configuration — capability
        bindings cannot be edited until it serves at least one.
      </p>
    );
  }

  return (
    <div className="mt-4">
      <div className="mb-1 flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-ink">Per-capability models</h4>
        <button
          type="button"
          disabled={autoconfigure.isPending}
          onClick={() => autoconfigure.mutate()}
          title="Pick the best available model for every capability, testing each one first"
          className="shrink-0 border border-line-strong px-2 py-1 text-[11px] font-medium text-ink-soft hover:bg-elevated disabled:opacity-50"
        >
          {autoconfigure.isPending ? "Testing models…" : "Configure from gateway"}
        </button>
      </div>
      <p className="mb-2 text-xs text-ink-muted">
        Options come from the gateway itself, filtered to the models that can actually do each job.
        Prices are the gateway&apos;s own rates.
      </p>
      {autoconfigure.isError && (
        <p className="mb-2 text-xs text-bad">{errMsg(autoconfigure.error)}</p>
      )}
      {autoconfigure.data && (
        <div className="mb-2 border border-line bg-elevated px-2 py-1.5 text-[11px] text-ink-soft">
          <div>Bound {Object.keys(autoconfigure.data.bound).length} capabilities.</div>
          {autoconfigure.data.unbound.length > 0 && (
            <div className="text-badge-warning-fg">
              No model for: {autoconfigure.data.unbound.join(", ")}.
            </div>
          )}
          {Object.keys(autoconfigure.data.unreachable).length > 0 && (
            <div className="text-badge-warning-fg">
              Advertised but unreachable, so skipped:{" "}
              {Object.keys(autoconfigure.data.unreachable).join(", ")}.
            </div>
          )}
        </div>
      )}
      <ul className="space-y-2">
        {CAPABILITIES.map((cap) => {
          const eligible = available.filter((m) => m.capabilities.includes(cap));
          const bound = profile?.bindings?.[cap]?.model;
          const pending = bind.isPending && bind.variables?.capability === cap;
          return (
            <li key={cap} className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-xs font-medium text-ink">{cap.replace(/_/g, " ")}</div>
                <div className="truncate text-[11px] text-ink-muted">{CAPABILITY_HELP[cap]}</div>
              </div>
              {eligible.length === 0 ? (
                <span
                  className="shrink-0 text-[11px] text-badge-warning-fg"
                  title="No model on this gateway meets this capability's requirements. It is left unbound on purpose — binding a model that cannot do the job fails later, and less visibly."
                >
                  unsupported by gateway
                </span>
              ) : (
                <select
                  aria-label={`Model for ${cap.replace(/_/g, " ")}`}
                  className="max-w-[62%] shrink-0 border border-line-strong bg-surface px-1.5 py-1 text-[11px] text-ink disabled:opacity-50"
                  value={bound ?? ""}
                  disabled={pending}
                  onChange={(e) => {
                    const model = eligible.find((m) => m.id === e.target.value);
                    if (model) bind.mutate({ capability: cap, model });
                  }}
                >
                  {bound === undefined && <option value="">— unbound —</option>}
                  {eligible.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.id} · {priceLabel(m)}
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

function ConnectorsSection({ projectId }: { projectId: string }) {
  const connectors = useQuery<ConnectorOut[]>({
    queryKey: ["connectors"],
    queryFn: () => api.get<ConnectorOut[]>(`/v1/connectors`),
  });
  const bindings = useQuery<ConnectorBindingOut[]>({
    queryKey: ["connector-bindings", projectId],
    queryFn: () => api.get<ConnectorBindingOut[]>(`/v1/projects/${projectId}/connectors/bindings`),
  });
  const creds = useQuery<CredentialOut[]>({
    queryKey: ["connector-credentials", projectId],
    queryFn: () => api.get<CredentialOut[]>(`/v1/projects/${projectId}/connector-credentials`),
  });

  if (!connectors.data) return null;
  const bindingByConnector = new Map(bindings.data?.map((b) => [b.connector_id, b]) ?? []);
  const credByKind = new Map(creds.data?.map((c) => [c.connector_kind, c]) ?? []);

  return (
    <Section title="Connectors">
      <p className="mb-2 text-xs text-ink-muted">
        Data sources the researcher can search. Enable a connector and, if it needs a key, add one
        here — keys are encrypted per-project and never leave the server.
      </p>
      <ul className="space-y-2">
        {connectors.data.map((c) => (
          <ConnectorRow
            key={c.id}
            projectId={projectId}
            connector={c}
            binding={bindingByConnector.get(c.id)}
            cred={credByKind.get(c.kind)}
          />
        ))}
      </ul>
    </Section>
  );
}

function ConnectorRow({
  projectId,
  connector,
  binding,
  cred,
}: {
  projectId: string;
  connector: ConnectorOut;
  binding: ConnectorBindingOut | undefined;
  cred: CredentialOut | undefined;
}) {
  const qc = useQueryClient();
  const [keyDraft, setKeyDraft] = useState("");
  const [editing, setEditing] = useState(false);
  const enabled = binding ? binding.enabled : connector.enabled_by_default;

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["connector-bindings", projectId] });
    void qc.invalidateQueries({ queryKey: ["connector-credentials", projectId] });
  };

  const toggle = useMutation({
    mutationFn: (next: boolean) =>
      api.post(`/v1/projects/${projectId}/connectors/bindings`, {
        connector_id: connector.id,
        enabled: next,
        config_jsonb: binding?.config_jsonb ?? {},
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
    mutationFn: () =>
      api.del(`/v1/projects/${projectId}/connector-credentials/${connector.kind}`),
    onSuccess: invalidate,
  });

  const hasKey = cred?.has_project_specific ?? false;
  return (
    <li className="border border-line px-3 py-2">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-ink">{connector.name}</div>
          <div className="text-[10px] uppercase tracking-wider text-ink-muted">
            {connector.kind}
            {connector.requires_auth && " · key required"}
            {cred?.status && ` · ${cred.status}`}
          </div>
        </div>
        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={`${enabled ? "Disable" : "Enable"} ${connector.name}`}
          disabled={toggle.isPending}
          onClick={() => toggle.mutate(!enabled)}
          className={
            "relative h-5 w-9 shrink-0 transition-colors disabled:opacity-50 " +
            (enabled ? "bg-good" : "bg-line-strong")
          }
        >
          <span
            className={
              "absolute top-0.5 h-4 w-4 bg-surface transition-transform " +
              (enabled ? "translate-x-4" : "translate-x-0.5")
            }
          />
        </button>
      </div>

      {connector.requires_auth && (
        <div className="mt-2">
          {hasKey && !editing ? (
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
                placeholder={hasKey ? "New key…" : "Paste API key…"}
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
              {hasKey && (
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
          {saveKey.isError && (
            <p className="mt-1 text-xs text-bad">{errMsg(saveKey.error)}</p>
          )}
        </div>
      )}
      {(toggle.isError || removeKey.isError) && (
        <p className="mt-1 text-xs text-bad">{errMsg(toggle.error ?? removeKey.error)}</p>
      )}
    </li>
  );
}

function errMsg(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 403) return "Owner access required.";
    return `Failed (${err.status}).`;
  }
  return "Something went wrong.";
}

interface LedgerEvent {
  id: string;
  actor_kind: string;
  action_kind: string;
  target_kind: string | null;
  target_id: string | null;
  trace_id: string | null;
  timestamp: string;
}

function LogsBody({ projectId }: { projectId: string }) {
  const ledger = useQuery<LedgerEvent[]>({
    queryKey: ["ledger", projectId],
    queryFn: () => api.get<LedgerEvent[]>(`/v1/projects/${projectId}/ledger?limit=50`),
    refetchInterval: 5_000,
  });
  if (!ledger.data) return <p className="text-ink-muted">Loading…</p>;
  if (ledger.data.length === 0) return <p className="text-ink-muted">No events yet.</p>;
  return (
    <ul className="space-y-1.5">
      {ledger.data.map((e) => (
        <li key={e.id} className="border border-line px-3 py-2 text-xs">
          <div className="flex items-center justify-between">
            <span className="font-medium text-ink">{e.action_kind}</span>
            <span className="text-ink-muted">{new Date(e.timestamp).toLocaleTimeString()}</span>
          </div>
          <div className="mt-1 text-ink-muted">
            <span className="font-medium">{e.actor_kind}</span>
            {e.target_kind && <span> → {e.target_kind}</span>}
          </div>
          {e.trace_id && (
            <div className="mt-1 truncate font-mono text-[10px] text-ink-muted" title={e.trace_id}>
              trace: {e.trace_id.slice(0, 16)}…
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

interface AgentRunOut {
  id: string;
  agent_kind: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  correlation_id: string;
  error_text: string | null;
  created_at: string;
}

function NotificationsBody({ projectId }: { projectId: string }) {
  const runs = useQuery<AgentRunOut[]>({
    queryKey: ["agent-runs", projectId, "drawer"],
    queryFn: () => api.get<AgentRunOut[]>(`/v1/projects/${projectId}/agent-runs?limit=25`),
    refetchInterval: 3_000,
  });
  if (!runs.data) return <p className="text-ink-muted">Loading…</p>;
  const failed = runs.data.filter((r) => r.status === "failed");
  const active = runs.data.filter((r) => r.status === "running" || r.status === "pending");
  const succeeded = runs.data.filter((r) => r.status === "succeeded");
  return (
    <div className="space-y-5">
      {failed.length > 0 && (
        <Section title={`Failed (${failed.length})`}>
          {failed.map((r) => (
            <div key={r.id} className="border border-line bg-badge-failed-bg px-3 py-2 text-xs">
              <div className="font-medium text-badge-failed-fg">{r.agent_kind}</div>
              {r.error_text && <div className="mt-1 text-badge-failed-fg">{r.error_text}</div>}
              <div className="mt-1 text-badge-failed-fg">
                {new Date(r.created_at).toLocaleString()}
              </div>
            </div>
          ))}
        </Section>
      )}
      <Section title={`Running (${active.length})`}>
        {active.length === 0 && <p className="text-ink-muted">No active agents.</p>}
        {active.map((r) => (
          <div key={r.id} className="border border-line bg-badge-running-bg px-3 py-2 text-xs">
            <div className="font-medium text-badge-running-fg">{r.agent_kind}</div>
            <div className="mt-1 text-badge-running-fg">{r.status}</div>
          </div>
        ))}
      </Section>
      <Section title={`Recent succeeded (${succeeded.length})`}>
        {succeeded.length === 0 && <p className="text-ink-muted">None yet.</p>}
        {succeeded.slice(0, 8).map((r) => (
          <div key={r.id} className="border border-line px-3 py-2 text-xs">
            <div className="font-medium text-ink">{r.agent_kind}</div>
            <div className="mt-1 text-ink-muted">
              {r.completed_at ? new Date(r.completed_at).toLocaleString() : "—"}
            </div>
          </div>
        ))}
      </Section>
    </div>
  );
}

function ProfileBody({ projectId }: { projectId: string }) {
  const me = useQuery<MeOut>({
    queryKey: ["me"],
    queryFn: () => api.get<MeOut>("/v1/me"),
  });
  const cost = useQuery<CostRollupOut>({
    queryKey: ["cost", projectId],
    queryFn: () => api.get<CostRollupOut>(`/v1/projects/${projectId}/cost`),
    refetchInterval: 30_000,
  });
  if (!me.data) return <p className="text-ink-muted">Loading…</p>;
  const usage = cost.data;
  const spent = usage ? Number(usage.total_usd) : 0;
  return (
    <div className="space-y-5">
      <Section title="Signed in as">
        <Row label="Email" value={me.data.email || "—"} />
        <Row label="Subject" value={me.data.subject} mono />
        <Row label="Actor kind" value={me.data.actor_kind} />
        <Row label="User ID" value={me.data.user_id} mono />
      </Section>
      <Section title="Usage">
        {usage ? (
          <>
            <Row label="Spent to date" value={`$${spent.toFixed(4)}`} />
            {usage.by_phase.length > 0 && (
              <div className="mt-3">
                <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-ink-muted">
                  By capability
                </p>
                {usage.by_phase.map((b) => (
                  <Row
                    key={b.key}
                    label={b.key}
                    value={`$${Number(b.cost_usd).toFixed(4)} · ${b.call_count} calls`}
                  />
                ))}
              </div>
            )}
          </>
        ) : (
          <p className="text-ink-muted">Loading usage…</p>
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-ink-muted">{title}</h3>
      <div className="space-y-1">{children}</div>
    </section>
  );
}

function Row({
  label,
  value,
  mono = false,
  multiline = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
  multiline?: boolean;
}) {
  // Multiline values (e.g. the project description) stack the value below the
  // label and wrap fully instead of truncating to a single line.
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

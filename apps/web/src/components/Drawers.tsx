import { useQuery } from "@tanstack/react-query";

import { api, type CostRollupOut, type MeOut, type ProjectOut } from "@/lib/api";
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
      <div className="flex-1 bg-slate-900/30" />
      <div
        className="flex h-full w-[28rem] flex-col border-l border-slate-200 bg-white shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-slate-200 px-5 py-3">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-700">
            {TITLES[kind]}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-slate-500 hover:bg-slate-100"
            aria-label="Close"
          >
            ✕
          </button>
        </header>
        <div className="flex-1 overflow-y-auto px-5 py-4 text-sm text-slate-700">
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
  if (!project.data) return <p className="text-slate-400">Loading…</p>;
  const p = project.data;
  return (
    <div className="space-y-5">
      <Section title="Project">
        <Row label="Title" value={p.title} />
        <Row label="Description" value={p.description || "—"} multiline />
        <Row label="Status" value={p.status} />
        <Row label="Created" value={new Date(p.created_at).toLocaleString()} />
      </Section>
      <Section title="Budget">
        {cost.data ? (
          <>
            <Row label="Cap (USD)" value={`$${Number(cost.data.cap_usd).toFixed(2)}`} />
            <Row label="Spent" value={`$${Number(cost.data.spent_usd).toFixed(4)}`} />
            <Row label="Soft cap %" value={`${Number(cost.data.soft_pct).toFixed(0)}%`} />
            <Row label="Hard cap %" value={`${Number(cost.data.hard_pct).toFixed(0)}%`} />
          </>
        ) : (
          <p className="text-slate-400">Loading cost…</p>
        )}
      </Section>
      <Section title="Members">
        {members.data?.length === 0 && <p className="text-slate-400">No members.</p>}
        <ul className="space-y-1">
          {members.data?.map((m) => (
            <li key={m.id} className="flex items-center justify-between">
              <span className="truncate font-mono text-xs">{m.user_id}</span>
              <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wider">
                {m.role}
              </span>
            </li>
          ))}
        </ul>
      </Section>
      <Section title="Model profile">
        <Row label="Profile ID" value={p.model_profile_id} mono />
      </Section>
    </div>
  );
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
  if (!ledger.data) return <p className="text-slate-400">Loading…</p>;
  if (ledger.data.length === 0) return <p className="text-slate-400">No events yet.</p>;
  return (
    <ul className="space-y-1.5">
      {ledger.data.map((e) => (
        <li key={e.id} className="rounded-md border border-slate-200 px-3 py-2 text-xs">
          <div className="flex items-center justify-between">
            <span className="font-medium text-slate-900">{e.action_kind}</span>
            <span className="text-slate-400">{new Date(e.timestamp).toLocaleTimeString()}</span>
          </div>
          <div className="mt-1 text-slate-500">
            <span className="font-medium">{e.actor_kind}</span>
            {e.target_kind && <span> → {e.target_kind}</span>}
          </div>
          {e.trace_id && (
            <div className="mt-1 truncate font-mono text-[10px] text-slate-400" title={e.trace_id}>
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
  if (!runs.data) return <p className="text-slate-400">Loading…</p>;
  const failed = runs.data.filter((r) => r.status === "failed");
  const active = runs.data.filter((r) => r.status === "running" || r.status === "pending");
  const succeeded = runs.data.filter((r) => r.status === "succeeded");
  return (
    <div className="space-y-5">
      {failed.length > 0 && (
        <Section title={`Failed (${failed.length})`}>
          {failed.map((r) => (
            <div key={r.id} className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs">
              <div className="font-medium text-red-900">{r.agent_kind}</div>
              {r.error_text && <div className="mt-1 text-red-700">{r.error_text}</div>}
              <div className="mt-1 text-red-500">
                {new Date(r.created_at).toLocaleString()}
              </div>
            </div>
          ))}
        </Section>
      )}
      <Section title={`Running (${active.length})`}>
        {active.length === 0 && <p className="text-slate-400">No active agents.</p>}
        {active.map((r) => (
          <div key={r.id} className="rounded-md border border-blue-200 bg-blue-50 px-3 py-2 text-xs">
            <div className="font-medium text-blue-900">{r.agent_kind}</div>
            <div className="mt-1 text-blue-600">{r.status}</div>
          </div>
        ))}
      </Section>
      <Section title={`Recent succeeded (${succeeded.length})`}>
        {succeeded.length === 0 && <p className="text-slate-400">None yet.</p>}
        {succeeded.slice(0, 8).map((r) => (
          <div key={r.id} className="rounded-md border border-slate-200 px-3 py-2 text-xs">
            <div className="font-medium text-slate-900">{r.agent_kind}</div>
            <div className="mt-1 text-slate-500">
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
  if (!me.data) return <p className="text-slate-400">Loading…</p>;
  const usage = cost.data;
  const cap = usage ? Number(usage.cap_usd) : 0;
  const spent = usage ? Number(usage.spent_usd) : 0;
  const pct = usage && cap > 0 ? Math.min(100, (spent / cap) * 100) : 0;
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
            <Row label="Budget cap" value={`$${cap.toFixed(2)}`} />
            <Row label="Spent to date" value={`$${spent.toFixed(4)}`} />
            <Row label="Percent used" value={`${pct.toFixed(1)}%`} />
            {usage.by_phase.length > 0 && (
              <div className="mt-3">
                <p className="mb-1 text-[10px] font-medium uppercase tracking-wider text-slate-400">
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
          <p className="text-slate-400">Loading usage…</p>
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-500">{title}</h3>
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
        <span className="text-xs uppercase tracking-wider text-slate-400">{label}</span>
        <span className="whitespace-pre-wrap break-words text-sm text-slate-800">{value}</span>
      </div>
    );
  }
  return (
    <div className="flex items-baseline justify-between gap-3">
      <span className="text-xs uppercase tracking-wider text-slate-400">{label}</span>
      <span
        className={`truncate text-right text-sm text-slate-800 ${mono ? "font-mono text-xs" : ""}`}
        title={value}
      >
        {value}
      </span>
    </div>
  );
}

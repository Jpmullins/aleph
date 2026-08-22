import { useMutation, useQuery, useQueryClient } from"@tanstack/react-query";
import { useState } from"react";

import { AlephLogo } from"@/components/AlephLogo";
import { Modal } from "@/components/Modal";
import { ThemeToggle } from"@/components/ThemeToggle";
import { ApiError, api, type ModelProfileOut, type ProjectOut } from"@/lib/api";

interface Props {
  onOpen: (projectId: string) => void;
}

export function ProjectList({ onOpen }: Props) {
  const [showCreate, setShowCreate] = useState(false);
  // Deleted projects are hidden by default but must be *findable*. A deleted
  // project still holds its sources and wiki; without a way to see it, the only
  // route back is knowing its UUID — which is how a real research corpus became
  // unreachable after a stray delete.
  const [showDeleted, setShowDeleted] = useState(false);
  const projectsQuery = useQuery<ProjectOut[]>({
    queryKey: ["projects", showDeleted],
    queryFn: () =>
      api.get<ProjectOut[]>(`/v1/projects${showDeleted ?"?include_deleted=true" :""}`),
  });

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <header className="mb-10 flex items-end justify-between border-b border-line pb-6">
        <div className="flex items-center gap-4">
          <AlephLogo size={44} variant="emblem" className="text-accent" />
          <div className="flex flex-col gap-0.5">
            <span className="font-prose text-3xl font-light leading-none tracking-tight text-ink">
              Aleph
            </span>
            <span className="font-mono text-[10px] uppercase tracking-[0.18em] text-ink-muted">
              a workbench that grows
            </span>
          </div>
        </div>
        <ThemeToggle />
      </header>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-prose text-2xl font-normal tracking-tight text-ink">
            Projects
          </h1>
          <p className="mt-1 max-w-[60ch] text-sm text-ink-soft">
            Each project is its own body of evidence — sources, the claims drawn
            from them, and an agent that can extend both.
          </p>
        </div>
        <label className="mr-3 flex cursor-pointer items-center gap-1.5 text-xs text-ink-muted">
          <input
            type="checkbox"
            checked={showDeleted}
            onChange={(e) => setShowDeleted(e.target.checked)}
            data-testid="show-deleted-projects"
          />
          Show deleted
        </label>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className="shrink-0 bg-accent px-4 py-2 font-mono text-xs uppercase tracking-[0.1em] text-accent-fg hover:bg-accent-hover"
        >
          + New project
        </button>
      </div>
      {projectsQuery.isPending && <p className="text-ink-muted">Loading projects…</p>}
      {projectsQuery.isError && (
        <p className="text-bad">
          Failed to load projects: {(projectsQuery.error as ApiError).message}
        </p>
      )}
      {projectsQuery.isSuccess && projectsQuery.data.length === 0 && (
        <div className="border border-dashed border-line-strong p-14 text-center">
          <p className="text-sm text-ink-muted">
            Nothing here yet. A project is where sources, claims and the agent meet.
          </p>
        </div>
      )}
      <ul className="flex flex-col gap-px bg-line">
        {projectsQuery.data?.map((p) => (
          <ProjectRow key={p.id} project={p} onOpen={onOpen} />
        ))}
      </ul>
      {showCreate && (
        <ProjectCreateModal
          onClose={() => setShowCreate(false)}
          onCreated={(id) => {
            setShowCreate(false);
            onOpen(id);
          }}
        />
      )}
    </div>
  );
}

function ProjectRow({
  project,
  onOpen,
}: {
  project: ProjectOut;
  onOpen: (id: string) => void;
}) {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const archive = useMutation({
    mutationFn: async () =>
      api.patch<ProjectOut>(`/v1/projects/${project.id}`, { status:"deleted" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  // The counterpart to delete, and the reason writes to a deleted project can
  // safely 409: there is a visible way back.
  const restore = useMutation({
    mutationFn: async () =>
      api.patch<ProjectOut>(`/v1/projects/${project.id}`, { status:"active" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  return (
    <li className="bg-surface transition-colors hover:bg-sunken">
      <div className="flex items-stretch">
        <button
          type="button"
          onClick={() => onOpen(project.id)}
          className="flex-1 px-4 py-3 text-left"
          data-testid={`project-open-${project.id}`}
        >
          <div className="flex items-center gap-2.5">
            <span
              aria-hidden
              className="h-3.5 w-[3px] shrink-0"
              style={{
                background:
                  project.status ==="deleted" ?"var(--state-bad)" :"var(--accent)",
              }}
            />
            <span className="text-[15px] text-ink">{project.title}</span>
          </div>
          <div className="mt-1 pl-[22px] font-mono text-[10.5px] text-ink-muted">
            {project.status} · {new Date(project.created_at).toLocaleDateString()}
          </div>
        </button>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="border-l border-line px-3 font-mono text-[10.5px] uppercase tracking-[0.08em] text-ink-muted hover:text-ink"
          data-testid={`project-info-${project.id}`}
          title="Project info"
          aria-expanded={expanded}
        >
          {expanded ?"Info −" :"Info +"}
        </button>
        {project.status ==="deleted" ? (
          <button
            type="button"
            onClick={() => restore.mutate()}
            disabled={restore.isPending}
            className="border-l border-line px-4 font-mono text-[10.5px] uppercase tracking-[0.08em] text-good disabled:opacity-50"
            data-testid={`project-restore-${project.id}`}
            title="Restore this project"
          >
            {restore.isPending ?"…" :"Restore"}
          </button>
        ) : (
          <button
            type="button"
            onClick={() => {
              if (
                window.confirm(
                  `Delete project"${project.title}"? Its sources and wiki are kept, ` +
                    `and you can restore it from"Show deleted".`,
                )
              ) {
                archive.mutate();
              }
            }}
            disabled={archive.isPending}
            className="border-l border-line px-4 text-xs font-medium text-ink-muted hover:bg-badge-failed-bg hover:text-badge-failed-fg disabled:opacity-50"
            data-testid={`project-delete-${project.id}`}
            title="Delete project"
          >
            {archive.isPending ?"…" :"Delete"}
          </button>
        )}
      </div>
      {expanded && (
        <div className="space-y-1.5 border-t border-line bg-sunken px-4 py-3 text-xs text-ink-soft">
          <p className="whitespace-pre-wrap break-words">
            <span className="font-medium text-ink-muted">Description: </span>
            {project.description ||"—"}
          </p>
          <p>
            <span className="font-medium text-ink-muted">Created: </span>
            {new Date(project.created_at).toLocaleString()}
          </p>
          <p className="font-mono text-[11px] text-ink-muted">{project.id}</p>
        </div>
      )}
    </li>
  );
}

interface CreateProps {
  onClose: () => void;
  onCreated: (id: string) => void;
}

function ProjectCreateModal({ onClose, onCreated }: CreateProps) {
  const qc = useQueryClient();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  /**
   * The profile names come from the server.
   *
   * They were a two-member union typed into this file — the two seeded template
   * names, spelled as string literals — which is a client-side copy of a list
   * only the database knows. Seed a third and it could not be chosen here; drop
   * one and this offered a name `POST /v1/projects` would reject. WS-B1's third
   * criterion is that no such copy survives anywhere under `apps/web/src`.
   *
   * `undefined` until the list lands, so the request carries no profile name at
   * all rather than a guessed one — the server's own default is a better answer
   * than this component's.
   */
  const templates = useQuery<ModelProfileOut[]>({
    queryKey: ["model-profile-templates"],
    queryFn: () => api.get<ModelProfileOut[]>("/v1/model-profile-templates"),
  });
  const names = templates.data?.map((t) => t.name) ?? [];
  const [profile, setProfile] = useState<string>("");
  const chosen = profile || names[0] || "";
  const create = useMutation({
    mutationFn: async () =>
      api.post<ProjectOut>("/v1/projects", {
        title,
        description,
        ...(chosen ? { model_profile_name: chosen } : {}),
      }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      onCreated(p.id);
    },
  });
  return (
    <Modal title="New project" onClose={onClose} testId="project-create-modal">
      <form
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
          className="space-y-4"
        >
          <label className="block">
            <span className="text-sm font-medium text-ink-soft">Title</span>
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="mt-1 w-full border border-line-strong px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium text-ink-soft">Description</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="mt-1 w-full border border-line-strong px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium text-ink-soft">Model profile</span>
            <select
              value={chosen}
              disabled={names.length === 0}
              onChange={(e) => setProfile(e.target.value)}
              className="mt-1 w-full border border-line-strong px-3 py-2 text-sm"
            >
              {names.length === 0 && (
                <option value="">
                  {templates.isPending ? "Loading templates…" : "No templates — server default"}
                </option>
              )}
              {names.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
          {create.isError && (
            <p className="text-sm text-bad">{(create.error as ApiError).message}</p>
          )}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="border border-line-strong px-4 py-2 text-sm hover:border-line-strong"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={create.isPending || !title}
              className="bg-ink px-4 py-2 text-sm font-medium text-ink-inverse hover:bg-ink-soft disabled:opacity-50"
            >
              {create.isPending ?"Creating…" :"Create"}
            </button>
          </div>
      </form>
    </Modal>
  );
}

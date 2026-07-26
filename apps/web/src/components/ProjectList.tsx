import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { AlephLogo } from "@/components/AlephLogo";
import { ThemeToggle } from "@/components/ThemeToggle";
import { ApiError, api, type ProjectOut } from "@/lib/api";

interface Props {
  onOpen: (projectId: string) => void;
}

export function ProjectList({ onOpen }: Props) {
  const [showCreate, setShowCreate] = useState(false);
  const projectsQuery = useQuery<ProjectOut[]>({
    queryKey: ["projects"],
    queryFn: () => api.get<ProjectOut[]>("/v1/projects"),
  });

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <header className="mb-10 flex items-center justify-between border-b border-line pb-6">
        <AlephLogo size={44} tagline="Multi-agent research environment" />
        <ThemeToggle />
      </header>
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">
            Projects
          </h1>
          <p className="mt-1 text-sm text-ink-muted">
            Each project is a self-contained research workspace — sources, a
            compiled wiki, and an assistant that can research and grow it.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreate(true)}
          className="shrink-0 rounded-md px-4 py-2 text-sm font-medium text-ink-inverse shadow hover:opacity-90"
          style={{ background: "var(--accent, #f97316)" }}
        >
          + New project
        </button>
      </div>
      {projectsQuery.isPending && <p className="text-ink-muted">Loading projects…</p>}
      {projectsQuery.isError && (
        <p className="text-red-700">
          Failed to load projects: {(projectsQuery.error as ApiError).message}
        </p>
      )}
      {projectsQuery.isSuccess && projectsQuery.data.length === 0 && (
        <div className="rounded-lg border border-dashed border-line-strong p-12 text-center">
          <p className="text-ink-muted">No projects yet. Create one to get started.</p>
        </div>
      )}
      <ul className="space-y-2">
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
      api.patch<ProjectOut>(`/v1/projects/${project.id}`, { status: "deleted" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["projects"] }),
  });

  return (
    <li className="overflow-hidden rounded-md border border-line bg-surface shadow-sm hover:border-line-strong">
      <div className="flex items-stretch">
        <button
          type="button"
          onClick={() => onOpen(project.id)}
          className="flex-1 px-4 py-3 text-left"
          data-testid={`project-open-${project.id}`}
        >
          <div className="font-medium text-ink">{project.title}</div>
          <div className="mt-1 text-xs text-ink-muted">
            {project.status} · created {new Date(project.created_at).toLocaleDateString()}
          </div>
        </button>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="border-l border-line px-3 text-xs font-medium text-ink-muted hover:bg-sunken hover:text-ink"
          data-testid={`project-info-${project.id}`}
          title="Project info"
          aria-expanded={expanded}
        >
          {expanded ? "▴ Info" : "▾ Info"}
        </button>
        <button
          type="button"
          onClick={() => {
            if (window.confirm(`Delete project "${project.title}"? This is reversible by an admin.`)) {
              archive.mutate();
            }
          }}
          disabled={archive.isPending}
          className="border-l border-line px-4 text-xs font-medium text-ink-muted hover:bg-red-50 hover:text-red-700 disabled:opacity-50"
          data-testid={`project-delete-${project.id}`}
          title="Delete project"
        >
          {archive.isPending ? "…" : "Delete"}
        </button>
      </div>
      {expanded && (
        <div className="space-y-1.5 border-t border-line bg-sunken px-4 py-3 text-xs text-ink-soft">
          <p className="whitespace-pre-wrap break-words">
            <span className="font-medium text-ink-muted">Description: </span>
            {project.description || "—"}
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
  const [profile, setProfile] = useState<"aleph-dev" | "aleph-production">("aleph-dev");
  const create = useMutation({
    mutationFn: async () =>
      api.post<ProjectOut>("/v1/projects", {
        title,
        description,
        model_profile_name: profile,
      }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ["projects"] });
      onCreated(p.id);
    },
  });
  return (
    <div className="fixed inset-0 flex items-center justify-center bg-ink/40 px-4">
      <div className="w-full max-w-md rounded-lg bg-surface p-6 shadow-xl">
        <h2 className="mb-4 text-xl font-semibold">New project</h2>
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
              className="mt-1 w-full rounded-md border border-line-strong px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium text-ink-soft">Description</span>
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              rows={3}
              className="mt-1 w-full rounded-md border border-line-strong px-3 py-2 text-sm"
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium text-ink-soft">Model profile</span>
            <select
              value={profile}
              onChange={(e) => setProfile(e.target.value as typeof profile)}
              className="mt-1 w-full rounded-md border border-line-strong px-3 py-2 text-sm"
            >
              <option value="aleph-dev">aleph-dev (cheap)</option>
              <option value="aleph-production">aleph-production (premium)</option>
            </select>
          </label>
          {create.isError && (
            <p className="text-sm text-red-600">{(create.error as ApiError).message}</p>
          )}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-line-strong px-4 py-2 text-sm hover:border-line-strong"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={create.isPending || !title}
              className="rounded-md bg-ink px-4 py-2 text-sm font-medium text-ink-inverse hover:bg-ink-soft disabled:opacity-50"
            >
              {create.isPending ? "Creating…" : "Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

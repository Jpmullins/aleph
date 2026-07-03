import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ApiError } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";

interface Props {
  projectId: string;
  onClose: () => void;
  onUploaded: () => void;
}

export function SourceUploadModal({ projectId, onClose, onUploaded }: Props) {
  const qc = useQueryClient();
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");

  const upload = useMutation({
    mutationFn: async () => {
      if (!file) throw new Error("file required");
      const fd = new FormData();
      fd.append("file", file);
      if (title) fd.append("title", title);
      const token = await getAccessToken();
      const baseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
      const resp = await fetch(`${baseUrl}/v1/projects/${projectId}/sources/upload`, {
        method: "POST",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      });
      if (!resp.ok) {
        let body: unknown = null;
        try {
          body = await resp.json();
        } catch {
          body = await resp.text();
        }
        throw new ApiError(`upload failed: ${resp.status}`, resp.status, body);
      }
      return resp.json();
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sources", projectId] });
      onUploaded();
    },
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 px-4">
      <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl">
        <h2 className="mb-4 text-xl font-semibold">Upload source</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            upload.mutate();
          }}
          className="space-y-4"
        >
          <label className="block">
            <span className="text-sm font-medium text-slate-700">File</span>
            <input
              required
              type="file"
              accept=".pdf,.docx,.md,.txt,.html,.htm,.epub"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="mt-1 w-full text-sm"
            />
          </label>
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Title (optional)</span>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
            />
          </label>
          {upload.isError && (
            <p className="text-sm text-red-600">
              {(upload.error as ApiError).message}
            </p>
          )}
          <div className="flex justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-slate-300 px-4 py-2 text-sm hover:border-slate-500"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={!file || upload.isPending}
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {upload.isPending ? "Uploading…" : "Upload"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

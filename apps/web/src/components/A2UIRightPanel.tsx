import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { A2UIComponent } from "@/a2ui/catalog";
import { renderA2UI } from "@/a2ui/register";
import { api } from "@/lib/api";

const TABS = ["Wiki", "Artifacts", "Notes", "Hypotheses", "Briefs"] as const;
type Tab = (typeof TABS)[number];

interface Props {
  projectId: string;
}

export function A2UIRightPanel({ projectId }: Props) {
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("Wiki");

  const surfaceQuery = useQuery<{ tab: string; surface: A2UIComponent }>({
    queryKey: ["surface", projectId, tab],
    queryFn: () =>
      api.get<{ tab: string; surface: A2UIComponent }>(
        `/v1/projects/${projectId}/surfaces/${tab.toLowerCase()}`,
      ),
    refetchInterval: tab === "Briefs" ? 10_000 : false,
  });

  const action = useMutation({
    mutationFn: async ({
      actionName,
      params,
    }: {
      actionName: string;
      params: Record<string, unknown>;
    }) =>
      api.post(`/v1/projects/${projectId}/cards/actions`, {
        surface_kind: `${tab}Surface`,
        action_kind: actionName,
        target_id: (params.target_id as string | undefined) ?? null,
        target_kind: (params.target_kind as string | undefined) ?? null,
        params,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["surface", projectId, tab] });
    },
  });

  return (
    <aside className="flex w-[28rem] flex-col border-l border-slate-200 bg-white">
      <nav className="flex border-b border-slate-200">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={
              "flex-1 px-2 py-2 text-xs font-medium " +
              (t === tab
                ? "border-b-2 border-slate-900 text-slate-900"
                : "text-slate-500 hover:text-slate-900")
            }
          >
            {t}
          </button>
        ))}
      </nav>
      <div className="flex-1 overflow-hidden">
        {surfaceQuery.isPending && (
          <div className="p-6 text-sm text-slate-500">Loading surface…</div>
        )}
        {surfaceQuery.data &&
          renderA2UI(surfaceQuery.data.surface, (actionName, params) =>
            action.mutate({ actionName, params }),
          )}
      </div>
    </aside>
  );
}

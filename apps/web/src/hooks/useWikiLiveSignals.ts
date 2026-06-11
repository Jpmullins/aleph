import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

/**
 * Live workspace signals from the push layer.
 *
 * Subscribes to `GET /v1/projects/{id}/changes/stream` (LISTEN/NOTIFY-backed) and
 * turns its signals into:
 *   - `compilingPages` — pages an agent is actively writing ("✦ editing…"),
 *   - `recentlyCommitted` — pages that just saved (the "updated just now" pulse),
 *   - targeted react-query invalidations per domain (`changed` signals), so
 *     EVERY tab's data refreshes the instant something writes — notes,
 *     hypotheses, artifacts, wiki sources — instead of waiting for its poll.
 *
 * Mount ONCE per workspace (the `LiveSignalsProvider` in ProjectWorkspace) so
 * there is a single EventSource regardless of the active tab; surface views
 * consume the result via `useLiveSignals()`.
 *
 * Pages are keyed by `page_id` when known, else `title:<title>` (a brand-new page
 * is signalled by title until its row exists; the commit signal carries the id).
 */
export interface WikiChangeSignal {
  kind: "committed" | "compiling" | "compile_done" | "changed";
  page_id?: string | null;
  page_title?: string | null;
  page_kind?: string;
  domain?: "notes" | "hypotheses" | "artifacts" | "wiki" | "briefs";
  actor_kind?: string;
  ts: string;
}

/** Which react-query key prefixes each `changed` domain invalidates. */
const DOMAIN_QUERY_KEYS: Record<string, string[][]> = {
  notes: [["notes"], ["note"]],
  hypotheses: [["hypotheses"], ["ach-matrix"]],
  artifacts: [["artifacts"], ["agent-runs"]],
  wiki: [["wiki-pages"]],
  briefs: [["surface"]],
};

export interface CompilingPage {
  pageTitle: string | null;
  since: number;
}

export interface WikiLiveSignals {
  /** Keyed by page_id or `title:<title>`. */
  compilingPages: Map<string, CompilingPage>;
  /** page_id → expiry epoch-ms (drives the brief "updated" highlight). */
  recentlyCommitted: Map<string, number>;
  /** True while any page is mid-compile — for a surface-level "building" hint. */
  isAgentBuilding: boolean;
}

const PULSE_MS = 3000;

function keyFor(sig: WikiChangeSignal): string | null {
  if (sig.page_id) return sig.page_id;
  if (sig.page_title) return `title:${sig.page_title}`;
  return null;
}

export function useWikiLiveSignals(projectId: string): WikiLiveSignals {
  const queryClient = useQueryClient();
  const [compilingPages, setCompilingPages] = useState<Map<string, CompilingPage>>(new Map());
  const [recentlyCommitted, setRecentlyCommitted] = useState<Map<string, number>>(new Map());
  const sinceRef = useRef<string>(new Date().toISOString());

  useEffect(() => {
    const baseUrl =
      (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
    const url = new URL(`${baseUrl}/v1/projects/${projectId}/changes/stream`);
    url.searchParams.set("since", sinceRef.current);
    const es = new EventSource(url.toString(), { withCredentials: false });

    es.addEventListener("change", (ev) => {
      let sig: WikiChangeSignal;
      try {
        sig = JSON.parse((ev as MessageEvent).data) as WikiChangeSignal;
      } catch {
        return;
      }
      sinceRef.current = sig.ts;

      if (sig.kind === "changed") {
        for (const key of DOMAIN_QUERY_KEYS[sig.domain ?? ""] ?? []) {
          queryClient.invalidateQueries({ queryKey: [...key, projectId] });
        }
        return;
      }

      const k = keyFor(sig);

      if (sig.kind === "compiling" && k) {
        setCompilingPages((prev) => {
          const next = new Map(prev);
          next.set(k, { pageTitle: sig.page_title ?? null, since: Date.now() });
          return next;
        });
        return;
      }

      if (sig.kind === "compile_done" && k) {
        setCompilingPages((prev) => {
          if (!prev.has(k)) return prev;
          const next = new Map(prev);
          next.delete(k);
          return next;
        });
        return;
      }

      if (sig.kind === "committed") {
        // Clear "editing" (by id and by title) and flash the "updated" pulse.
        setCompilingPages((prev) => {
          const next = new Map(prev);
          if (sig.page_id) next.delete(sig.page_id);
          if (sig.page_title) next.delete(`title:${sig.page_title}`);
          return next.size === prev.size ? prev : next;
        });
        if (sig.page_id) {
          const pageId = sig.page_id;
          setRecentlyCommitted((prev) => {
            const next = new Map(prev);
            next.set(pageId, Date.now() + PULSE_MS);
            return next;
          });
        }
        // Refresh the index and, crucially, the specific open page in place.
        queryClient.invalidateQueries({ queryKey: ["wiki-pages", projectId] });
        if (sig.page_id) {
          queryClient.invalidateQueries({ queryKey: ["wiki-page", projectId, sig.page_id] });
        }
      }
    });

    // EventSource reconnects automatically on error; nothing to do here.
    return () => es.close();
  }, [projectId, queryClient]);

  // Expire pulse highlights.
  useEffect(() => {
    if (recentlyCommitted.size === 0) return;
    const t = setInterval(() => {
      const now = Date.now();
      setRecentlyCommitted((prev) => {
        let changed = false;
        const next = new Map(prev);
        for (const [k, exp] of prev) {
          if (exp <= now) {
            next.delete(k);
            changed = true;
          }
        }
        return changed ? next : prev;
      });
    }, 1000);
    return () => clearInterval(t);
  }, [recentlyCommitted]);

  return {
    compilingPages,
    recentlyCommitted,
    isAgentBuilding: compilingPages.size > 0,
  };
}

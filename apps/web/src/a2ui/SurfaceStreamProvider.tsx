/**
 * One SSE connection for the whole workspace; panes read surfaces out of it.
 *
 * The workspace is a set of panes, and a connection per pane hits the browser's
 * ~6-per-origin HTTP/1.1 cap at four panes — with agent-events and wiki-signals
 * already open, three panes is the practical ceiling.
 *
 * Multiplexing is also *stronger*, not just cheaper. The server stamps one
 * monotonic `seq` per connection, so every pane shares a single total order.
 * Independent connections each have their own `seq` space, which means a page
 * and the claim view beside it could render mutually inconsistent states with
 * nothing detecting it.
 *
 * None of this needed protocol work: every A2UI message already carries
 * `surfaceId` and `MessageProcessor` already holds a `surfacesMap`. One surface
 * per connection was a UI constraint imposed on a multi-surface protocol.
 */
import { MessageProcessor, type SurfaceModel } from "@a2ui/web_core/v0_9";
import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  buildAlephCatalogs,
  type AlephComponentApi,
  type PluginCatalogDescriptor,
} from "./aleph-catalog-v09";
import { api } from "@/lib/api";


type AnySurface = SurfaceModel<AlephComponentApi>;

interface StreamCtx {
  /** `surfaceId` → live surface model. */
  surfaces: Map<string, AnySurface>;
  connected: boolean;
  /** Non-null when the stream failed in a way the user should know about. */
  error: string | null;
}

const Ctx = createContext<StreamCtx | null>(null);

function seqOf(msg: unknown): number | null {
  if (msg && typeof msg === "object" && "seq" in msg) {
    const s = (msg as { seq: unknown }).seq;
    return typeof s === "number" ? s : null;
  }
  return null;
}

export function SurfaceStreamProvider({
  projectId,
  panes,
  children,
}: {
  projectId: string;
  /** Pane ids, which double as wire `surfaceId`s. */
  panes: string[];
  children: ReactNode;
}) {
  // What catalogs this project's renderer should hold, from
  // `GET /v1/projects/{id}/catalogs` — core, core's legacy alias, and one per
  // enabled plugin. Starts at the static pair rather than at nothing, so a pane
  // never waits on a network call to paint: every surface the product itself
  // emits names core or its alias, and the plugin entries only widen the set.
  //
  // A failure is deliberately not raised here. The observable consequence of a
  // missing plugin catalog is that its surfaces answer `Catalog not found`,
  // which `onmessage` already reports through `setError` — a second error path
  // for the same fact would just be noisier.
  const [plugins, setPlugins] = useState<PluginCatalogDescriptor[]>([]);
  useEffect(() => {
    let live = true;
    void api
      .get<{ catalogs?: PluginCatalogDescriptor[] }>(`/v1/projects/${projectId}/catalogs`)
      .then((body) => {
        if (!live) return;
        // Replaced only when the SET actually changed. `catalogs` is a
        // dependency of the effect that opens the SSE stream, so handing back a
        // fresh array with identical contents would close the connection and
        // rebuild every surface — once per mount for a project with no plugins
        // at all, which is every project today.
        setPlugins((prev) => {
          const next = (body.catalogs ?? []).filter((c) => Boolean(c.plugin));
          const same =
            prev.length === next.length &&
            prev.every((p, i) => p.catalogId === next[i].catalogId);
          return same ? prev : next;
        });
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, [projectId]);

  const catalogs = useMemo(() => buildAlephCatalogs(plugins), [plugins]);
  const [surfaces, setSurfaces] = useState<Map<string, AnySurface>>(new Map());
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Stable per-mount so `EventSource`'s auto-reconnect reuses the same URL and
  // the server can replay only what was missed.
  const cidRef = useRef<string>("");
  if (!cidRef.current) {
    cidRef.current =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  const paneKey = panes.join(",");

  useEffect(() => {
    const processor = new MessageProcessor(catalogs);
    let live = true;
    let lastSeq = -1;

    const sync = () => {
      if (live) setSurfaces(new Map(processor.model.surfacesMap));
    };
    const created = processor.onSurfaceCreated(sync);
    const deleted = processor.onSurfaceDeleted(sync);

    const base =
      (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";
    const url =
      `${base}/v1/projects/${projectId}/surfaces/stream` +
      `?panes=${encodeURIComponent(paneKey)}&cid=${encodeURIComponent(cidRef.current)}`;

    const es = new EventSource(url, { withCredentials: false });
    es.onopen = () => {
      if (live) {
        setConnected(true);
        setError(null);
      }
    };
    es.onmessage = (ev) => {
      if (!live || !ev.data) return;
      try {
        const msg = JSON.parse(ev.data) as unknown;
        const seq = seqOf(msg);
        if (seq !== null) {
          if (seq <= lastSeq) return; // duplicate / out-of-order
          lastSeq = seq;
        }
        processor.processMessages([msg] as never);
        sync();
      } catch (err) {
        // A duplicate `createSurface` after reconnect throws here and is
        // expected. Anything else is surfaced rather than silently dropped —
        // swallowing every frame error is how a dead panel looks like an empty
        // one.
        const m = err instanceof Error ? err.message : String(err);
        if (!/already exists/i.test(m) && live) setError(m);
      }
    };
    es.onerror = () => {
      // EventSource retries with backoff on its own; report the gap so the UI
      // can say "reconnecting" rather than showing stale data as current.
      if (live) setConnected(false);
    };

    return () => {
      live = false;
      es.close();
      created.unsubscribe();
      deleted.unsubscribe();
    };
  }, [catalogs, projectId, paneKey]);

  const value = useMemo<StreamCtx>(
    () => ({ surfaces, connected, error }),
    [surfaces, connected, error],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSurfaceStream(): StreamCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useSurfaceStream must be used inside a SurfaceStreamProvider");
  return ctx;
}

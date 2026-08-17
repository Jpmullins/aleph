/**
 * Wave 4 — reusable v0.9 surface renderer.
 *
 * Feeds a list of A2UI v0.9 server-to-client messages (as emitted by the
 * backend `aleph_a2ui.messages` builders) into a `MessageProcessor` against the
 * shared Aleph catalog (`aleph-catalog-v09`) + the basic-catalog primitives,
 * and renders every resulting surface with `<A2uiSurface>`. This is the exact
 * wiring proven by the Task 1 spike (`_spike/SpikePanel.tsx`), extracted so both
 * the right panel and (later) Live chat consume one code path.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { MessageProcessor, Catalog, SurfaceModel } from "@a2ui/web_core/v0_9";
import { A2uiSurface, basicCatalog } from "@a2ui/react/v0_9";

import { ALEPH_CARD_IMPLS } from "./aleph-catalog-v09";
import { SurfaceProvider } from "./surface-context";

/** Catalog id the backend's `createSurface.catalogId` references. */
export const ALEPH_V09_CATALOG_ID = "aleph://v1";

/**
 * Read the monotonic sequence stamped on every surface SSE message (WP-4). The
 * server stamps `seq` (and mirrors it as the SSE `id:`); the client applies
 * strictly-increasing seqs and drops duplicates / out-of-order frames so a
 * reconnect replay or a racing delivery can never apply state out of order.
 */
export function surfaceSeq(msg: unknown): number | null {
  if (msg && typeof msg === "object" && "seq" in msg) {
    const s = (msg as { seq: unknown }).seq;
    return typeof s === "number" ? s : null;
  }
  return null;
}

/** Append a stable per-connection id so the server's replay ring survives an
 * `EventSource` auto-reconnect (same URL, same `cid`). */
function withConnectionId(streamUrl: string, cid: string): string {
  try {
    const u = new URL(streamUrl);
    u.searchParams.set("cid", cid);
    return u.toString();
  } catch {
    const sep = streamUrl.includes("?") ? "&" : "?";
    return `${streamUrl}${sep}cid=${encodeURIComponent(cid)}`;
  }
}

/**
 * One shared catalog: ALL of Aleph's domain impls (13 cards + 5 surfaces, from
 * `ALEPH_CARD_IMPLS`) + every basic-catalog primitive (Column/Row/Text/...) so
 * agents/builders can compose layout around the cards.
 *
 * Built once per mount via `useMemo`. The catalog is pure config (no per-surface
 * state), so it's safe to reuse across the throwaway processors below.
 */
export function buildAlephCatalog() {
  return new Catalog(
    ALEPH_V09_CATALOG_ID,
    [...ALEPH_CARD_IMPLS, ...basicCatalog.components.values()],
    [],
  );
}

/** The concrete component-api type the shared catalog carries (React impls). */
type AlephComponentApi =
  ReturnType<typeof buildAlephCatalog> extends Catalog<infer T> ? T : never;
type AlephSurface = SurfaceModel<AlephComponentApi>;

interface Props {
  /** Ordered v0.9 message list (createSurface, updateComponents, updateDataModel…). */
  messages: unknown[];
  /**
   * Project + surface context for the rendered cards/surfaces. Aleph's existing
   * views call `useSurface()` (from `surface-context.tsx`) to fetch live data
   * (wiki pages, dataset rows, hypotheses, …) and POST feedback, so the rendered
   * tree must sit inside a `SurfaceProvider`. (Task 7 re-homed this context off
   * `register.tsx` into `surface-context.tsx`.)
   */
  projectId: string;
  surface: string;
}

/**
 * `MessageProcessor` is STATEFUL: it accumulates surfaces, and a second
 * `createSurface` for an already-registered surfaceId throws
 * `A2uiStateError: Surface <id> already exists`. React 19 StrictMode runs every
 * effect twice on mount (setup → cleanup → setup) and the component can also
 * re-render with the same `messages` identity, so we must guarantee a given
 * processor instance only ever ingests a message set ONCE.
 *
 * The robust approach: derive the processor from `messages` and build a FRESH
 * instance every time the effect (re-)runs. Each effect invocation processes the
 * full message set into its own pristine processor, so there is never a stale
 * surface to collide with — the StrictMode double-invoke just throws away the
 * first processor and rebuilds, and a same-`messages` re-run is harmless because
 * it starts from an empty processor again.
 */
export function A2UISurfaceView({ messages, projectId, surface }: Props) {
  const catalog = useMemo(() => buildAlephCatalog(), []);
  const [surfaces, setSurfaces] = useState<AlephSurface[]>([]);

  useEffect(() => {
    // Fresh processor for this exact message set — guarantees no pre-existing
    // surface, so `createSurface` can never collide (StrictMode-safe).
    const processor = new MessageProcessor([catalog]);
    let live = true;
    const sync = () => {
      if (live) setSurfaces(Array.from(processor.model.surfacesMap.values()));
    };
    const createdSub = processor.onSurfaceCreated(sync);
    const deletedSub = processor.onSurfaceDeleted(sync);
    // The processor's typed message union is built against zod v3; our wire
    // messages match the v0.9 shape at runtime. Cast at the boundary (as the
    // spike does).
    processor.processMessages(messages as never);
    sync();
    return () => {
      // Tear down before the next (StrictMode or messages-change) run so we
      // never double-subscribe or write state from a discarded processor.
      live = false;
      createdSub.unsubscribe();
      deletedSub.unsubscribe();
    };
  }, [catalog, messages]);

  if (surfaces.length === 0) {
    return <div className="p-6 text-sm text-ink-muted">No surface.</div>;
  }
  return (
    <SurfaceProvider projectId={projectId} surface={surface}>
      {surfaces.map((s) => (
        <A2uiSurface key={s.id} surface={s} />
      ))}
    </SurfaceProvider>
  );
}

interface StreamProps {
  /** SSE endpoint emitting the full v0.9 surface on connect + `updateDataModel` deltas. */
  streamUrl: string;
  projectId: string;
  surface: string;
}

/**
 * Wave 4 T6 — delta-consuming surface view.
 *
 * Opens the `…/surfaces/{tab}/stream` SSE endpoint and feeds EVERY message
 * (the full `createSurface`/`updateComponents`/root-`updateDataModel` on
 * connect, then incremental `updateDataModel` deltas) into ONE persistent
 * `MessageProcessor` for the life of the connection. Because the processor
 * persists, a leaf delta like `{updateDataModel, path:"/items/0/confidence"}`
 * patches the existing surface's data model in place — the upstream binder
 * re-renders only the bound prop, NOT the whole tree (existing card DOM nodes
 * are preserved). A new card arrives as an `updateComponents` (adds the new
 * component id; existing ids are updated in place, never torn down) followed by
 * an `add` data delta seeding its `/items/N`.
 *
 * The StrictMode-safe pattern from the one-shot view still applies, but the
 * unit of "process once" is now the whole CONNECTION: we create the processor
 * once per effect run, attach it to a fresh EventSource, and tear both down on
 * cleanup (tab switch / unmount / StrictMode re-run). A re-run opens a fresh
 * connection whose first messages rebuild the surface from scratch into a
 * pristine processor, so `createSurface` can never collide.
 */
export function A2UIStreamSurfaceView({ streamUrl, projectId, surface }: StreamProps) {
  const catalog = useMemo(() => buildAlephCatalog(), []);
  const [surfaces, setSurfaces] = useState<AlephSurface[]>([]);
  // One connection id per mount. `EventSource` auto-reconnect reuses the same
  // URL (hence the same cid), so the server can replay only missed deltas.
  const cidRef = useRef<string>("");
  if (!cidRef.current) {
    cidRef.current =
      typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  useEffect(() => {
    const processor = new MessageProcessor([catalog]);
    let live = true;
    // Ordering guard: last applied seq for THIS connection (survives an
    // EventSource auto-reconnect within this effect run).
    let lastSeq = -1;
    const sync = () => {
      if (live) setSurfaces(Array.from(processor.model.surfacesMap.values()));
    };
    const createdSub = processor.onSurfaceCreated(sync);
    const deletedSub = processor.onSurfaceDeleted(sync);

    const es = new EventSource(withConnectionId(streamUrl, cidRef.current), {
      withCredentials: false,
    });
    es.onmessage = (ev) => {
      if (!live || !ev.data) return; // heartbeats are SSE comments → never onmessage
      try {
        const msg = JSON.parse(ev.data) as unknown;
        const seq = surfaceSeq(msg);
        if (seq !== null) {
          if (seq <= lastSeq) return; // duplicate / out-of-order → drop
          lastSeq = seq;
        }
        processor.processMessages([msg] as never);
        sync();
      } catch {
        /* ignore malformed frame */
      }
    };
    es.onerror = () => {
      // EventSource auto-reconnects with backoff. On reconnect the server
      // resends the full surface; the duplicate `createSurface` throws inside
      // `processMessages` and is swallowed by the per-message try/catch, while
      // the resent `updateComponents`/`updateDataModel` re-apply harmlessly to
      // the already-registered surface. So a transient blip self-heals.
    };

    return () => {
      live = false;
      es.close();
      createdSub.unsubscribe();
      deletedSub.unsubscribe();
    };
  }, [catalog, streamUrl]);

  if (surfaces.length === 0) {
    return <div className="p-6 text-sm text-ink-muted">Loading surface…</div>;
  }
  return (
    <SurfaceProvider projectId={projectId} surface={surface}>
      {surfaces.map((s) => (
        <A2uiSurface key={s.id} surface={s} />
      ))}
    </SurfaceProvider>
  );
}

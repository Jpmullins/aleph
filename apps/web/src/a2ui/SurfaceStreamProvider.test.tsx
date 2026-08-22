/**
 * One SSE connection for the whole reading region.
 *
 * The transport is the thing every pane depends on and the thing no pane can
 * see. Three properties are pinned here because each fails silently:
 *
 *   - **one connection, not one per pane.** Four panes plus agent-events plus
 *     wiki-signals exceeds the browser's ~6-per-origin HTTP/1.1 cap, and the
 *     seventh request does not error — it waits, so a pane simply never fills.
 *   - **the pane set reaches the URL.** The server reads `?panes=` to decide
 *     what to build; a pane missing from that string renders an empty frame
 *     with nothing reporting a problem.
 *   - **a replayed frame is dropped, an unknown error is not.** Swallowing
 *     every frame error is how a dead panel looks like an empty one.
 *
 * The A2UI `MessageProcessor` is faked. The subject here is the transport, and
 * the real processor drags in the whole card catalog (vega-embed included) to
 * assert nothing this file is about.
 */
import { act, render } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/** Every catalog list a `MessageProcessor` was constructed with, in order.
 *
 *  Recorded because nothing asserted it. Changing
 *  `buildAlephCatalogs(plugins)` to `buildAlephCatalogs()` — ignoring every
 *  fetched plugin catalog, so a plugin's surfaces render as "Catalog not
 *  found" — left `check-single-catalog.sh`, lint, build and all 172 vitest
 *  tests green, because the fetch was mocked and never asserted on. */
const processorCatalogs: { id: string }[][] = [];

vi.mock("@a2ui/web_core/v0_9", () => {
  class FakeProcessor {
    model = { surfacesMap: new Map<string, unknown>() };
    constructor(catalogs: { id: string }[] = []) {
      processorCatalogs.push(catalogs);
    }
    processMessages(
      messages: { surfaceId?: string; boom?: string; deleteSurface?: { surfaceId: string } }[],
    ) {
      for (const msg of messages) {
        if (msg.boom) throw new Error(msg.boom);
        // The real processor's `deleteSurface`. Modelled here because the
        // provider is now the thing that sends it — keeping the processor
        // across a pane-set change means closed panes have to be pruned, and a
        // fake that ignored the message would report a leak as a pass.
        if (msg.deleteSurface) {
          this.model.surfacesMap.delete(msg.deleteSurface.surfaceId);
          continue;
        }
        if (msg.surfaceId) this.model.surfacesMap.set(msg.surfaceId, msg);
      }
    }
    onSurfaceCreated() {
      return { unsubscribe: () => undefined };
    }
    onSurfaceDeleted() {
      return { unsubscribe: () => undefined };
    }
  }
  return { MessageProcessor: FakeProcessor };
});

vi.mock("@/a2ui/aleph-catalog-v09", () => ({
  buildAlephCatalogs: (plugins: { catalogId: string }[] = []) => [
    { id: "aleph://core@1" },
    { id: "aleph://v1" },
    ...plugins.map((p) => ({ id: p.catalogId })),
  ],
}));

// The provider asks the server which catalogs this project should hold. Stubbed
// per test rather than left to jsdom's absent `fetch`: an unhandled rejection
// here would fail an unrelated assertion three tests later.
const catalogsResponse = vi.fn(async () => ({ catalogs: [] as unknown[] }));
vi.mock("@/lib/api", () => ({ api: { get: () => catalogsResponse() } }));

import { SurfaceStreamProvider, useSurfaceStream } from "@/a2ui/SurfaceStreamProvider";

interface FakeSource {
  url: string;
  closed: boolean;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onerror: (() => void) | null;
}

let sources: FakeSource[] = [];

class FakeEventSource {
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    sources.push(this as unknown as FakeSource);
  }
  close() {
    this.closed = true;
  }
}

beforeEach(() => {
  sources = [];
  processorCatalogs.length = 0;
  catalogsResponse.mockResolvedValue({ catalogs: [] as unknown[] });
  // jsdom ships no EventSource at all, so this is not a convenience — without
  // it the provider throws on mount and the whole reading region is untestable.
  vi.stubGlobal("EventSource", FakeEventSource);
});

function Probe() {
  const { surfaces, connected, error } = useSurfaceStream();
  return (
    <div>
      <span data-testid="ids">{[...surfaces.keys()].sort().join("|")}</span>
      <span data-testid="connected">{String(connected)}</span>
      <span data-testid="error">{error ?? ""}</span>
    </div>
  );
}

function mount(panes: string[]) {
  return render(
    <SurfaceStreamProvider projectId="proj-1" panes={panes}>
      <Probe />
    </SurfaceStreamProvider>,
  );
}

/**
 * Deliver one SSE frame.
 *
 * Wrapped in `act` because the provider answers a frame with `setState`, and an
 * unwrapped update leaves the assertion reading the PREVIOUS render — which
 * fails as "the frame did nothing" and sends you looking at the provider.
 */
function frame(source: FakeSource, payload: unknown) {
  act(() => {
    source.onmessage?.({ data: JSON.stringify(payload) });
  });
}

describe("SurfaceStreamProvider", () => {
  it("opens ONE connection for many panes", () => {
    mount(["wiki", "wiki:page_id=abc", "notes", "grounding"]);
    expect(sources).toHaveLength(1);
  });

  it("names every pane in the subscription so the server knows what to build", () => {
    mount(["wiki", "wiki:page_id=abc"]);
    const url = new URL(sources[0].url);
    expect(url.pathname).toBe("/v1/projects/proj-1/surfaces/stream");
    expect(url.searchParams.get("panes")).toBe("wiki,wiki:page_id=abc");
  });

  it("carries a connection id, so a reconnect can be replayed rather than restarted", () => {
    mount(["wiki"]);
    expect(new URL(sources[0].url).searchParams.get("cid")).toBeTruthy();
  });

  it("routes frames for different panes into one shared surface map", () => {
    // This is what multiplexing has to mean. One connection that only ever
    // delivered the first pane would pass the count assertion above and leave
    // every other pane blank.
    const view = mount(["wiki", "notes"]);
    frame(sources[0], { seq: 1, surfaceId: "wiki" });
    frame(sources[0], { seq: 2, surfaceId: "notes" });
    expect(view.getByTestId("ids").textContent).toBe("notes|wiki");
  });

  it("drops a replayed or out-of-order frame instead of re-applying it", () => {
    const view = mount(["wiki", "notes"]);
    frame(sources[0], { seq: 5, surfaceId: "wiki" });
    frame(sources[0], { seq: 3, surfaceId: "notes" });
    expect(view.getByTestId("ids").textContent).toBe("wiki");
  });

  it("swallows the duplicate-surface error a reconnect always produces", () => {
    const view = mount(["wiki"]);
    frame(sources[0], { seq: 1, boom: "surface wiki already exists" });
    expect(view.getByTestId("error").textContent).toBe("");
  });

  it("surfaces any other frame error rather than rendering an empty pane", () => {
    const view = mount(["wiki"]);
    frame(sources[0], { seq: 1, boom: "catalog mismatch" });
    expect(view.getByTestId("error").textContent).toBe("catalog mismatch");
  });

  it("reports the gap when the stream drops, so stale data is not shown as current", () => {
    const view = mount(["wiki"]);
    act(() => sources[0].onopen?.());
    expect(view.getByTestId("connected").textContent).toBe("true");
    act(() => sources[0].onerror?.());
    expect(view.getByTestId("connected").textContent).toBe("false");
  });

  it("closes the connection on unmount", () => {
    const view = mount(["wiki"]);
    view.unmount();
    expect(sources[0].closed).toBe(true);
  });

  it("re-subscribes once when the pane set changes, closing the old connection", () => {
    const view = mount(["wiki"]);
    view.rerender(
      <SurfaceStreamProvider projectId="proj-1" panes={["wiki", "notes"]}>
        <Probe />
      </SurfaceStreamProvider>,
    );
    expect(sources).toHaveLength(2);
    expect(sources[0].closed).toBe(true);
    expect(new URL(sources[1].url).searchParams.get("panes")).toBe("wiki,notes");
  });

  /**
   * WS-B1a c5. Opening a pane must not rebuild the panes already open.
   *
   * `new MessageProcessor(catalogs)` sat inside the connection effect, and
   * `paneKey` is one of that effect's dependencies — so opening the sixteenth
   * pane discarded `surfacesMap` and all fifteen surfaces in it, along with
   * every component tree and every value bound into them. The stream then had
   * to re-deliver the lot, and until it did those panes rendered "waiting for
   * the first frame…". Nothing failed, so nothing said anything.
   *
   * Counting constructions is the assertion because that is the mechanism. The
   * existing "re-subscribes once" test above passes either way: the connection
   * SHOULD be rebuilt, and it was never the expensive half.
   */
  it("does not rebuild the message processor when a pane is opened", () => {
    const view = mount(["wiki"]);
    frame(sources[0], { seq: 1, surfaceId: "wiki" });
    const built = processorCatalogs.length;
    expect(built).toBeGreaterThan(0);

    view.rerender(
      <SurfaceStreamProvider projectId="proj-1" panes={["wiki", "notes"]}>
        <Probe />
      </SurfaceStreamProvider>,
    );

    expect(processorCatalogs).toHaveLength(built);
    // And therefore the surface that was already open is still there, rather
    // than waiting on the new connection to re-send it.
    expect(view.getByTestId("ids").textContent).toBe("wiki");
  });

  it("drops the surface of a pane that was closed, so the map cannot grow forever", () => {
    // The cost of keeping the processor. A closed pane's surface renders
    // nowhere — the Board maps over the PANES — so this leak is invisible from
    // the screen and has to be pinned here or not at all.
    const view = mount(["wiki", "notes"]);
    frame(sources[0], { seq: 1, surfaceId: "wiki" });
    frame(sources[0], { seq: 2, surfaceId: "notes" });
    expect(view.getByTestId("ids").textContent).toBe("notes|wiki");

    view.rerender(
      <SurfaceStreamProvider projectId="proj-1" panes={["wiki"]}>
        <Probe />
      </SurfaceStreamProvider>,
    );
    expect(view.getByTestId("ids").textContent).toBe("wiki");
  });

  it("refuses to be read outside its provider instead of reporting an empty workspace", () => {
    expect(() => render(<Probe />)).toThrow(/SurfaceStreamProvider/);
  });
});


// ---------------------------------------------------------------------------
// The plugin catalogs the server names must reach the renderer
// ---------------------------------------------------------------------------
//
// `GET /v1/projects/{id}/catalogs`, the `Boolean(c.plugin)` filter, the
// identity-stable `setPlugins`, and the array reaching `MessageProcessor` were
// all untested. The fetch was mocked with a response nothing ever overrode and
// nothing ever asserted on, so the whole path could be deleted silently.

describe("plugin catalogs", () => {
  it("hands a fetched plugin catalog to the message processor", async () => {
    catalogsResponse.mockResolvedValue({
      catalogs: [{ catalogId: "aleph://plugin/charts@1", plugin: "charts" }],
    });

    await act(async () => {
      mount(["wiki"]);
      await Promise.resolve();
    });

    const last = processorCatalogs.at(-1) ?? [];
    expect(last.map((c) => c.id)).toContain("aleph://plugin/charts@1");
  });

  it("drops a catalog the server does not attribute to a plugin", async () => {
    // `plugin` is what distinguishes a plugin's catalog from core's. Without
    // the filter, core would be handed to the processor twice under two ids.
    catalogsResponse.mockResolvedValue({
      catalogs: [
        { catalogId: "aleph://core@1" },
        { catalogId: "aleph://plugin/charts@1", plugin: "charts" },
      ],
    });

    await act(async () => {
      mount(["wiki"]);
      await Promise.resolve();
    });

    const ids = (processorCatalogs.at(-1) ?? []).map((c) => c.id);
    expect(ids).toContain("aleph://plugin/charts@1");
    // Exactly one core entry — the one `buildAlephCatalogs` always emits.
    expect(ids.filter((id) => id === "aleph://core@1")).toHaveLength(1);
  });

  it("does not rebuild the stream when the catalog set is unchanged", async () => {
    // `catalogs` is a dependency of the effect that opens the SSE connection,
    // so handing back a fresh array with identical contents closes the stream
    // and rebuilds every surface. The provider compares before replacing; this
    // is what would notice if it stopped.
    catalogsResponse.mockResolvedValue({
      catalogs: [{ catalogId: "aleph://plugin/charts@1", plugin: "charts" }],
    });

    await act(async () => {
      mount(["wiki"]);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(sources.filter((s) => !s.closed)).toHaveLength(1);
  });
});

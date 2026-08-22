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

vi.mock("@a2ui/web_core/v0_9", () => {
  class FakeProcessor {
    model = { surfacesMap: new Map<string, unknown>() };
    processMessages(messages: { surfaceId?: string; boom?: string }[]) {
      for (const msg of messages) {
        if (msg.boom) throw new Error(msg.boom);
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

vi.mock("@/a2ui/aleph-catalog-v09", () => ({ buildAlephCatalog: () => ({ id: "aleph://v1" }) }));

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

  it("refuses to be read outside its provider instead of reporting an empty workspace", () => {
    expect(() => render(<Probe />)).toThrow(/SurfaceStreamProvider/);
  });
});

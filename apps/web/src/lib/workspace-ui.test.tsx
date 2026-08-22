/**
 * The pane model — the thing the workspace IS.
 *
 * Every rule pinned here was written in response to a defect that shipped, and
 * each is invisible from the outside: a duplicate pane looks like clutter the
 * user caused, a stale `activeSurface` looks like the agent misunderstanding,
 * and an `openPageId` that never reaches the stream URL looks like a page that
 * "just doesn't open" while every layer reports success.
 */
import { act, render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MAX_PANES, WorkspaceUIProvider, useWorkspaceUI, type WorkspaceUIState } from "@/lib/workspace-ui";

/**
 * Drive the context directly.
 *
 * The alternative is to click through `Rail` and `Board`, which tests the pane
 * reducer through two renderers that can each hide a defect in it. The reducer
 * is the subject; the components have their own files.
 */
function mountWorkspace(): () => WorkspaceUIState {
  let latest: WorkspaceUIState | null = null;
  function Probe() {
    latest = useWorkspaceUI();
    return null;
  }
  render(
    <WorkspaceUIProvider>
      <Probe />
    </WorkspaceUIProvider>,
  );
  return () => {
    if (!latest) throw new Error("probe never rendered");
    return latest;
  };
}

describe("the pane model", () => {
  it("starts with NO panes, because naming a starting one is naming a surface", () => {
    // It used to boot holding `{ id: "wiki", kind: "Wiki" }`. That is the
    // client deciding a research-suite surface is what a workbench opens with,
    // which is the whole thing `GET /panes` exists to abolish — and it is why
    // `check-pane-registry.sh` had to tolerate one registry id per file. `Rail`
    // seeds the board from the server's own list; see Rail.test.tsx.
    const ui = mountWorkspace();
    expect(ui().panes).toHaveLength(0);
    expect(ui().focusedPaneId).toBe("");
    expect(ui().activeSurface).toBe("");
  });

  it("stores a pane's kind as the server's id, whatever case it was asked for", () => {
    // The action router still answers with title-cased tab names ("Wiki",
    // "Grounding"). Stored raw, `openPane("Wiki")` and `openPane("wiki")` are
    // two panes claiming one id and the duplicate guard below matches neither.
    const ui = mountWorkspace();
    act(() => ui().openPane("Wiki"));
    act(() => ui().openPane("wiki"));
    expect(ui().panes.map((p) => p.kind)).toEqual(["wiki"]);
    expect(ui().panes.map((p) => p.id)).toEqual(["wiki"]);
  });

  it("keeps a pane id that is NOT its title's lower-case form", () => {
    // The defect this whole workstream is about. A plugin pane registered as
    // `dispute-queue` and titled "Dispute Queue" must round-trip as
    // `dispute-queue`: the pane id IS the wire `surfaceId`, and the server
    // silently drops a spec naming a tab it does not have.
    const ui = mountWorkspace();
    act(() => ui().openPane("dispute-queue", { title: "Dispute Queue" }));
    expect(ui().panes[0]).toMatchObject({
      id: "dispute-queue",
      kind: "dispute-queue",
      title: "Dispute Queue",
    });
  });

  it("re-opening the same view focuses it instead of duplicating it", () => {
    const ui = mountWorkspace();
    act(() => ui().openPane("notes"));
    act(() => ui().openPane("notes"));
    expect(ui().panes.filter((p) => p.kind === "notes")).toHaveLength(1);
    expect(ui().focusedPaneId).toBe("notes");
  });

  it("focuses the Wiki pane that is showing a page rather than opening a second one", () => {
    // The shipped defect: a card's `open` action re-keys the pane to
    // `wiki:page_id=…` and then asks for the surface by name. Matching on the
    // exact id alone opened a SECOND Wiki pane on every click.
    const ui = mountWorkspace();
    act(() => ui().openPane("wiki", { params: { page_id: "page-1" } }));
    const before = ui().panes.filter((p) => p.kind === "wiki").length;
    act(() => ui().openPane("wiki"));
    expect(ui().panes.filter((p) => p.kind === "wiki")).toHaveLength(before);
  });

  it("keys a pane by (kind, params), so two pages are two panes", () => {
    const ui = mountWorkspace();
    act(() => ui().openPane("wiki", { params: { page_id: "page-1" } }));
    act(() => ui().openPane("wiki", { params: { page_id: "page-2" } }));
    const ids = ui().panes.map((p) => p.id);
    expect(ids).toContain("wiki:page_id=page-1");
    expect(ids).toContain("wiki:page_id=page-2");
  });

  it("opening a page puts the page id in the pane id, which is the wire surfaceId", () => {
    // A page held only in React state never reached the server: the stream
    // subscribes with `?panes=<id>`, the server reads `page_id` out of that
    // spec, and a page opened purely in state left `/open` null. Clicking a
    // page then did visibly nothing while every layer reported success.
    //
    // `setOpenPageId` no longer opens the pane itself — it cannot, without
    // naming the wiki surface. The caller that HAS the pane name (the card
    // action's own navigate result) opens it; see navigate.test.tsx.
    const ui = mountWorkspace();
    act(() => ui().openPane("wiki", { params: { page_id: "abc-123" } }));
    expect(ui().focusedPaneId).toBe("wiki:page_id=abc-123");
    expect(ui().panes.some((p) => p.id === "wiki:page_id=abc-123")).toBe(true);
  });

  it("setOpenPageId records the open page and opens nothing", () => {
    const ui = mountWorkspace();
    act(() => ui().setOpenPageId("abc-123"));
    expect(ui().openPageId).toBe("abc-123");
    expect(ui().panes).toHaveLength(0);
  });

  it("derives activeSurface from the focused pane rather than storing it", () => {
    // Held separately, the two drifted: the rail opened a pane and focused it
    // while `activeSurface` stayed on Wiki, so the context bar — and the
    // `active_tab` the agent is told about — named a surface nobody was looking
    // at.
    const ui = mountWorkspace();
    act(() => ui().openPane("wiki"));
    act(() => ui().openPane("library"));
    expect(ui().activeSurface).toBe("library");
    act(() => ui().setFocusedPaneId("wiki"));
    expect(ui().activeSurface).toBe("wiki");
  });

  it("setActiveSurface opens or focuses the pane instead of swapping a slot", () => {
    const ui = mountWorkspace();
    act(() => ui().setActiveSurface("Hypotheses"));
    expect(ui().panes.map((p) => p.kind)).toContain("hypotheses");
    expect(ui().activeSurface).toBe("hypotheses");
  });

  it("closing the last pane leaves the stage empty rather than re-naming one", () => {
    // The fallback that used to sit here re-created `{id:"wiki"}` by name. The
    // Board has an empty state and the Rail re-seeds from the SERVER's list.
    const ui = mountWorkspace();
    act(() => ui().openPane("wiki"));
    act(() => ui().closePane("wiki"));
    expect(ui().panes).toHaveLength(0);
    expect(ui().focusedPaneId).toBe("");
  });

  it("closing the focused pane moves focus rather than leaving it dangling", () => {
    const ui = mountWorkspace();
    act(() => ui().openPane("wiki"));
    act(() => ui().openPane("notes"));
    expect(ui().focusedPaneId).toBe("notes");
    act(() => ui().closePane("notes"));
    expect(ui().panes.some((p) => p.id === "notes")).toBe(false);
    expect(ui().panes.some((p) => p.id === ui().focusedPaneId)).toBe(true);
  });

  it("caps open panes at 24", () => {
    // The literal is here on purpose. The ceiling is a runaway guard — a stuck
    // agent loop must not be able to open blocks forever — so changing it is a
    // decision, and a decision should show up as a diff in a test rather than
    // as one character in a constant.
    expect(MAX_PANES).toBe(24);
  });

  it("opening past the cap evicts the oldest rather than refusing the open", () => {
    // Refusing would be the wrong trade: the analyst asked for this view and
    // should get it. What must not happen is unbounded growth.
    const ui = mountWorkspace();
    act(() => {
      for (let i = 0; i < MAX_PANES + 6; i += 1) {
        ui().openPane("wiki", { params: { page_id: `p${i}` } });
      }
    });
    expect(ui().panes).toHaveLength(MAX_PANES);
    expect(ui().panes.at(0)?.id).toBe("wiki:page_id=p6");
    expect(ui().panes.at(-1)?.id).toBe(`wiki:page_id=p${MAX_PANES + 5}`);
    expect(ui().focusedPaneId).toBe(`wiki:page_id=${`p${MAX_PANES + 5}`}`);
  });

  it("refuses to be used outside its provider instead of returning an empty workspace", () => {
    function Orphan() {
      useWorkspaceUI();
      return null;
    }
    expect(() => render(<Orphan />)).toThrow(/WorkspaceUIProvider/);
  });
});

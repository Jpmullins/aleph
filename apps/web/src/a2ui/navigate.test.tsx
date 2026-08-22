/**
 * What a card's `open` action actually DOES to the workspace.
 *
 * Every card in `aleph-catalog-v09.tsx` is built from one wrapper, `adapt()`,
 * and the last thing that wrapper does is turn the server's `navigate` result
 * into a pane. That step had no test at all, and it is where "Open claim" died:
 * `ClaimCard` has emitted `open {target_kind:"claim"}` since it was written,
 * `_open` had no `claim` branch, the response carried no `tab`, and the handler
 * below did nothing. A button that posts a 200 and moves nothing looks
 * identical, from every layer's point of view, to a button that works.
 *
 * The one thing faked here is the HTTP response. Its SHAPE is pinned against
 * the real handler by `tests/integration/test_surface_open_claim.py`, which
 * drives `_open` through `ActionRouter.dispatch` against Postgres and asserts
 * the same `{tab, params:{claim_id}}`. Faking the transport and pinning the
 * fake against the producer is the only way to test the browser half without a
 * browser; asserting the fake alone would prove nothing.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn();
vi.mock("@/lib/api", () => ({
  api: {
    post: (path: string, body: unknown) => post(path, body),
    get: () => Promise.resolve({}),
  },
}));

import { adapt } from "@/a2ui/aleph-catalog-v09";
import { ClaimCard } from "@/a2ui/components/ClaimCard";
import { WikiPageCard } from "@/a2ui/components/WikiPageCard";
import { SurfaceProvider } from "@/a2ui/surface-context";
import { WorkspaceUIProvider, useWorkspaceUI, type Pane } from "@/lib/workspace-ui";

const CLAIM_ID = "11111111-1111-4111-8111-111111111111";
const PAGE_ID = "22222222-2222-4222-8222-222222222222";

let panes: Pane[] = [];
let openPageId: string | null = null;

function Probe() {
  const ui = useWorkspaceUI();
  panes = ui.panes;
  openPageId = ui.openPageId;
  return null;
}

function mount(Card: Parameters<typeof adapt>[1], name: string, props: Record<string, unknown>) {
  const Impl = adapt(name as Parameters<typeof adapt>[0], Card, "c");
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceUIProvider>
        <SurfaceProvider projectId="proj-1" surface="wikiSurface" paneKind="wiki">
          <Impl props={props} />
          <Probe />
        </SurfaceProvider>
      </WorkspaceUIProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  post.mockReset();
  panes = [];
  openPageId = null;
});

describe("a card's open action", () => {
  it("asks the server to open the CLAIM, naming the claim's kind", async () => {
    post.mockResolvedValue({ result: { navigate: {} } });
    const view = mount(ClaimCard, "ClaimCard", {
      claim_id: CLAIM_ID,
      text: "Chunks are written before the embed.",
      confidence: "supported",
    });
    fireEvent.click(view.getByText(/Open claim/));
    await waitFor(() => expect(post).toHaveBeenCalled());
    const [path, body] = post.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe("/v1/projects/proj-1/cards/actions");
    expect(body).toMatchObject({
      action_kind: "open",
      target_id: CLAIM_ID,
      target_kind: "claim",
    });
  });

  /**
   * The claim → chunk → char-span chain's only route in from the UI.
   *
   * The Grounding pane is `launchable: false` precisely because it is
   * meaningless without its parameter, so a navigate result that names the tab
   * and drops the parameter opens an EMPTY grounding surface — which renders
   * identically to a claim that has no evidence at all, the single thing that
   * pane exists to tell apart.
   */
  it("opens the grounding pane keyed on the claim, parameter and all", async () => {
    post.mockResolvedValue({
      result: {
        navigate: {
          target_id: CLAIM_ID,
          target_kind: "claim",
          tab: "Grounding",
          params: { claim_id: CLAIM_ID },
        },
      },
    });
    const view = mount(ClaimCard, "ClaimCard", {
      claim_id: CLAIM_ID,
      text: "Chunks are written before the embed.",
      confidence: "supported",
    });
    fireEvent.click(view.getByText(/Open claim/));
    await waitFor(() =>
      expect(panes.map((p) => p.id)).toContain(`grounding:claim_id=${CLAIM_ID}`),
    );
    // The pane id IS the wire `surfaceId`, and the server parses `claim_id`
    // straight back out of it — so the id carrying the claim is the whole
    // mechanism, not a cosmetic detail.
    const pane = panes.find((p) => p.id === `grounding:claim_id=${CLAIM_ID}`);
    expect(pane?.kind).toBe("grounding");
    expect(pane?.params).toEqual({ claim_id: CLAIM_ID });
  });

  it("still opens a wiki page from the older page_id-only navigate shape", async () => {
    // `navigate_wiki` and the `wiki_page` branch of `_open` answer with a bare
    // `page_id` beside the tab. It has to keep landing in the pane id, or a
    // clicked page opens the INDEX and the reader shows a list.
    post.mockResolvedValue({
      result: { navigate: { tab: "Wiki", page_id: PAGE_ID } },
    });
    const view = mount(WikiPageCard, "WikiPageCard", {
      page_id: PAGE_ID,
      title: "Distillation",
      body_md: "See [[Target Page]].",
      wikilinks_out: [{ dst_title: "Target Page", dst_page_id: PAGE_ID, occurrences: 1 }],
    });
    fireEvent.click(view.getByTestId("wikilink-chip"));
    await waitFor(() => expect(panes.map((p) => p.id)).toContain(`wiki:page_id=${PAGE_ID}`));
    expect(openPageId).toBe(PAGE_ID);
  });

  it("does nothing to the workspace when the server names no surface", async () => {
    // Unknown target kinds keep the legacy echo shape. Opening *something*
    // anyway would be worse than opening nothing: the analyst would be moved to
    // a surface that has nothing to do with what they clicked.
    post.mockResolvedValue({ result: { navigate: { target_kind: "mystery" } } });
    const view = mount(ClaimCard, "ClaimCard", {
      claim_id: CLAIM_ID,
      text: "…",
      confidence: "supported",
    });
    fireEvent.click(view.getByText(/Open claim/));
    await waitFor(() => expect(post).toHaveBeenCalled());
    expect(panes).toHaveLength(0);
  });
});

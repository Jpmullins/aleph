/**
 * The shared-context bar renders exactly the payload the agent is sent.
 *
 * Aleph already computed this — active surface, open page, selection — and sent
 * it to the agent every turn without ever showing it to the human, so
 * "summarize this page" worked by apparent magic and there was no way to notice
 * when the agent was looking at something stale. The bar closes that loop, which
 * means it must derive from the SAME state, with no second source of truth: a
 * bar that renders its own idea of the active surface is worse than no bar,
 * because it is a confident wrong answer.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, waitFor, type RenderResult } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
vi.mock("@/lib/api", () => ({ api: { get: (path: string) => get(path) } }));

import { ContextBar } from "@/components/ContextBar";
import { WorkspaceUIProvider, useWorkspaceUI, type WorkspaceUIState } from "@/lib/workspace-ui";

let ui: WorkspaceUIState | null = null;

function Probe() {
  ui = useWorkspaceUI();
  return null;
}

async function mountBar(titles: string[] = ["Wiki", "Notes", "Library"]): Promise<RenderResult> {
  get.mockResolvedValue({
    panes: titles.map((t) => ({
      id: t.toLowerCase(),
      title: t,
      icon: "notes",
      launchable: true,
      params: [],
    })),
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <WorkspaceUIProvider>
        <ContextBar projectId="proj-1" projectTitle="Photosynthesis" />
        <Probe />
      </WorkspaceUIProvider>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(get).toHaveBeenCalledWith("/v1/projects/proj-1/panes"));
  return view;
}

function state(): WorkspaceUIState {
  if (!ui) throw new Error("probe never rendered");
  return ui;
}

beforeEach(() => {
  get.mockReset();
  ui = null;
});

describe("ContextBar", () => {
  it("names the project", async () => {
    const view = await mountBar();
    expect(view.getByTestId("context-project").textContent).toBe("Photosynthesis");
  });

  it("reports the focused pane's kind, following the workspace rather than leading it", async () => {
    const view = await mountBar();
    act(() => state().openPane("Notes"));
    await waitFor(() => expect(view.getByTestId("context-active-tab").textContent).toBe("Notes"));
  });

  it("cycles through the surfaces the SERVER returned, not a compiled-in list", async () => {
    const view = await mountBar(["Wiki", "Notes", "Library"]);
    await waitFor(() => expect(view.getByTestId("context-active-tab").textContent).toBe("Wiki"));
    fireEvent.click(view.getByTestId("context-active-tab"));
    await waitFor(() => expect(view.getByTestId("context-active-tab").textContent).toBe("Notes"));
    fireEvent.click(view.getByTestId("context-active-tab"));
    await waitFor(() => expect(view.getByTestId("context-active-tab").textContent).toBe("Library"));
  });

  it("does nothing when the project offers no surfaces at all", async () => {
    const view = await mountBar([]);
    expect(() => fireEvent.click(view.getByTestId("context-active-tab"))).not.toThrow();
  });

  it("shows the open page and the live selection, the two least discoverable parts", async () => {
    const view = await mountBar();
    act(() => {
      state().setOpenPageTitle("Distillation");
      state().setSelection({ claim_id: null, page_id: null, text: "one sentence" });
    });
    await waitFor(() => expect(view.getByTestId("context-open-page").textContent).toBe("Distillation"));
    expect(view.getByTestId("context-selection").textContent).toContain("one sentence");
  });

  it("offers ingest only when the workspace passed a handler", async () => {
    const view = await mountBar();
    expect(view.queryByTestId("context-upload-source")).toBeNull();
  });
});

/**
 * The rail renders whatever the SERVER says this project can open.
 *
 * It used to compile in five names — Wiki, Library, Notes, Hypotheses, Briefs —
 * which is the research plugin suite, not a workbench. Install something
 * unrelated to papers and it had nowhere to appear; remove the research suite
 * and the rail still advertised it. `GET /v1/projects/{id}/panes` is now the
 * source, and the two failure modes worth pinning are both silent: a pane the
 * server marks unlaunchable appearing anyway, and an icon a plugin names that
 * this client does not ship taking the whole rail down at render.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, waitFor, type RenderResult } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn();
vi.mock("@/lib/api", () => ({ api: { get: (path: string) => get(path) } }));

import { Rail } from "@/components/Rail";
import { WorkspaceUIProvider, useWorkspaceUI, type Pane } from "@/lib/workspace-ui";

function pane(id: string, title: string, icon: string, launchable = true) {
  return { id, title, icon, launchable, params: [] };
}

let panes: Pane[] = [];

function PaneProbe() {
  panes = useWorkspaceUI().panes;
  return null;
}

async function mountRail(served: ReturnType<typeof pane>[]): Promise<RenderResult> {
  get.mockResolvedValue({ panes: served });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={client}>
      <WorkspaceUIProvider>
        <Rail projectId="proj-1" onBack={() => undefined} onOpenDrawer={() => undefined} />
        <PaneProbe />
      </WorkspaceUIProvider>
    </QueryClientProvider>,
  );
  await waitFor(() => expect(get).toHaveBeenCalledWith("/v1/projects/proj-1/panes"));
  return view;
}

beforeEach(() => {
  get.mockReset();
  panes = [];
});

describe("Rail", () => {
  it("offers exactly the launchable panes the server returned", async () => {
    const view = await mountRail([
      pane("wiki", "Wiki", "wiki"),
      pane("notes", "Notes", "notes"),
      pane("grounding", "Grounding", "grounding", false),
    ]);
    await waitFor(() => expect(view.getByTestId("rail-notes")).toBeTruthy());
    expect(view.queryByTestId("rail-grounding")).toBeNull();
  });

  it("shows a pane name this client has never heard of", async () => {
    // The whole point of a server-driven registry: a plugin installed after
    // this bundle was built has to be reachable without a redeploy.
    const view = await mountRail([pane("dispute-queue", "Dispute Queue", "wiki")]);
    await waitFor(() => expect(view.getByTestId("rail-dispute queue")).toBeTruthy());
  });

  it("falls back rather than throwing on an icon it does not ship", async () => {
    // `Icons[kind.icon]` is a runtime string lookup. Without the `?? Icons.notes`
    // fallback an unknown key is `undefined` used as a component, which throws
    // and takes the entire rail — every surface — with it.
    const view = await mountRail([pane("weather", "Weather", "no-such-icon")]);
    await waitFor(() => expect(view.getByTestId("rail-weather")).toBeTruthy());
  });

  it("opens a pane beside what is already there rather than replacing it", async () => {
    const view = await mountRail([pane("wiki", "Wiki", "wiki"), pane("notes", "Notes", "notes")]);
    await waitFor(() => expect(view.getByTestId("rail-notes")).toBeTruthy());
    fireEvent.click(view.getByTestId("rail-notes"));
    expect(panes.map((p) => p.kind)).toEqual(["Wiki", "Notes"]);
  });

  it("focuses an already-open pane instead of opening a second one", async () => {
    const view = await mountRail([pane("notes", "Notes", "notes")]);
    await waitFor(() => expect(view.getByTestId("rail-notes")).toBeTruthy());
    fireEvent.click(view.getByTestId("rail-notes"));
    fireEvent.click(view.getByTestId("rail-notes"));
    expect(panes.filter((p) => p.kind === "Notes")).toHaveLength(1);
  });

  it("marks the focused pane as current, so the rail is legible from the periphery", async () => {
    const view = await mountRail([pane("notes", "Notes", "notes")]);
    await waitFor(() => expect(view.getByTestId("rail-notes")).toBeTruthy());
    fireEvent.click(view.getByTestId("rail-notes"));
    await waitFor(() =>
      expect(view.getByTestId("rail-notes").getAttribute("aria-current")).toBe("page"),
    );
  });

  it("keeps the drawer buttons, which are not panes and are not server-driven", async () => {
    const view = await mountRail([]);
    for (const kind of ["settings", "logs", "notifications", "profile"]) {
      expect(view.getByTestId(`rail-${kind}`)).toBeTruthy();
    }
  });
});

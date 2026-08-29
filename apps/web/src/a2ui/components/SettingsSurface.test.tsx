/**
 * Every section kind the SERVER can emit has a renderer here.
 *
 * The join between `routes/surfaces.py` and `SectionBody`'s switch was pinned
 * by nothing. The integration tests assert the server BUILDS a section; the
 * per-component Vitest tests mount each section component DIRECTLY. Nothing
 * rendered `SettingsSurface` itself, so misspelling one `case` — dropping a
 * whole settings screen — left every test in the repository green. Found by
 * an adversarial pass that renamed `case "gateway_endpoints"` and watched
 * nothing notice.
 *
 * The kinds are listed here rather than read from the server, because a test
 * that derives its expectation from its subject asserts only that the subject
 * agrees with itself. If a new kind ships, `check-surface-bindings.sh` and
 * this list are the two places it has to be named.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SurfaceProvider } from "@/a2ui/surface-context";
import { WorkspaceUIProvider } from "@/lib/workspace-ui";
import { SettingsSurface } from "@/a2ui/components/SettingsSurface";

/** Every `"kind"` emitted by `_settings_surface` in routes/surfaces.py. */
const SERVER_KINDS = [
  "fields",
  "members",
  "gateway_endpoints",
  "model_profile",
  "connectors",
  "plugins",
  "ledger",
  "runs",
] as const;

function renderKinds(kinds: readonly string[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceUIProvider>
        <SurfaceProvider projectId="p1" surface="settings">
        <SettingsSurface
          // Never called: every section here renders read-only, and a section
          // that DID dispatch through the renderer would be the F7/F8 defect —
          // a settings value in `card_actions` and the ledger.
          onAction={() => {
            throw new Error("no section may dispatch a card action from the renderer");
          }}
          component={{
            type: "SettingsSurface",
            id: "settings-surface",
            props: {
              title: "Settings",
              sections: kinds.map((kind) => ({ kind, title: kind })),
            },
          }}
        />
        </SurfaceProvider>
      </WorkspaceUIProvider>
    </QueryClientProvider>,
  );
}

describe("SettingsSurface section dispatch", () => {
  it("renders every section kind the server can emit", () => {
    const { queryAllByTestId } = renderKinds(SERVER_KINDS);
    const unknown = queryAllByTestId("settings-unknown-section");
    expect(
      unknown.map((node) => node.textContent),
      "a kind the server sends has no renderer, so that settings screen is gone",
    ).toEqual([]);
  });

  it("says so, visibly, when a kind really has no renderer", () => {
    // The anti-vacuity half. Without it the assertion above passes for a
    // build whose fallback was deleted, which is the failure it exists to
    // report — a section quietly missing is indistinguishable from a setting
    // that was never offered.
    const { queryAllByTestId } = renderKinds(["a_kind_no_build_will_ever_have"]);
    expect(queryAllByTestId("settings-unknown-section")).toHaveLength(1);
  });
});

describe("SettingsSurface is navigable and readable", () => {
  /**
   * Settings is a long DOCUMENT rendered in a pane a few hundred pixels tall:
   * project, cost, members, model gateway, model profile with a control per
   * capability, connectors and plugin settings, in one column. Everything below
   * the second section was invisible until you scrolled for it, with nothing
   * indicating it was there — which is how "I cannot set the model endpoint"
   * happens while the endpoint control is on the same screen.
   */
  it("offers a jump link per section so a control below the fold is findable", () => {
    const { getByTestId } = renderKinds(SERVER_KINDS);
    const nav = getByTestId("settings-jump-nav");
    for (const kind of SERVER_KINDS) {
      expect(nav.textContent).toContain(kind);
    }
  });

  it("does not show a jump nav for a single section", () => {
    // Navigation for one destination is furniture, not help.
    const { queryByTestId } = renderKinds(["fields"]);
    expect(queryByTestId("settings-jump-nav")).toBeNull();
  });

  it("gives every section an id its jump link can reach", () => {
    const { getByTestId, container } = renderKinds(SERVER_KINDS);
    const nav = getByTestId("settings-jump-nav");
    const hrefs = [...nav.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs.length).toBe(SERVER_KINDS.length);
    for (const href of hrefs) {
      // A link to an id nothing carries scrolls nowhere and reads as broken.
      expect(container.querySelector(href!)).not.toBeNull();
    }
  });

  it("renders a stored timestamp as a date a person reads", () => {
    // The pane showed `2026-08-29T14:22:18.380650+00:00`. That is the value the
    // database holds and not the value anyone came to read.
    const { getByText } = renderRows([
      { label: "CREATED", value: "2026-08-29T14:22:18.380650+00:00" },
    ]);
    const cell = getByText(/2026/);
    expect(cell.textContent).not.toContain("T14:22:18");
    expect(cell.getAttribute("title")).toBe("2026-08-29T14:22:18.380650+00:00");
  });

  it("keeps the exact value in the title so an id stays copyable", () => {
    const { getByText } = renderRows([{ label: "SPENT (USD)", value: "$0.0000" }]);
    const cell = getByText("$0.00");
    expect(cell.getAttribute("title")).toBe("$0.0000");
  });

  it("leaves a value it does not recognise alone", () => {
    // Formatting must not be a guess: an unrecognised string is shown as stored.
    const { getByText } = renderRows([{ label: "STATUS", value: "active" }]);
    expect(getByText("active")).toBeTruthy();
  });
});

function renderRows(rows: { label: string; value: string }[]) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceUIProvider>
        <SurfaceProvider projectId="p1" surface="settings">
          <SettingsSurface
            onAction={() => {
              throw new Error("no section may dispatch a card action from the renderer");
            }}
            component={{
              type: "SettingsSurface",
              id: "settings-surface",
              props: { title: "Settings", sections: [{ kind: "fields", title: "Project", rows }] },
            }}
          />
        </SurfaceProvider>
      </WorkspaceUIProvider>
    </QueryClientProvider>,
  );
}

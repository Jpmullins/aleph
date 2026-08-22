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

/**
 * Embedded-child dispatch.
 *
 * A few surface views carry a structural `children` array of A2UI data objects
 * that the backend forwards inline. v0_9's binder resolves the surface
 * component's own props but does NOT walk that array, so the surface has to
 * dispatch each child to its card view by `type`.
 *
 * The failure this pins is the one the pane model cannot see: a `type` with no
 * entry in the table. `CARD_VIEWS[type]` is `undefined`, and `<undefined />`
 * throws — taking the whole surface down, not just the one card. The diagnostic
 * branch is what turns that into a visible, named, single-card failure, and it
 * only ever runs when something is already wrong, which is exactly the branch
 * nobody exercises by hand.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SurfaceProvider, renderChildCard, useSurface } from "@/a2ui/surface-context";
import type { A2UIComponent, ComponentName } from "@/a2ui/catalog";

function child(type: string, props: Record<string, unknown> = {}): A2UIComponent {
  return { type: type as ComponentName, id: "c-1", props };
}

describe("renderChildCard", () => {
  it("names the component it could not render instead of throwing", () => {
    const view = render(<>{renderChildCard(child("NotAComponent"), () => undefined)}</>);
    expect(view.container.textContent).toContain("Unknown A2UI component: NotAComponent");
  });

  it("renders a known card type", () => {
    const view = render(
      <SurfaceProvider projectId="proj-1" surface="Wiki">
        {renderChildCard(
          child("ClaimCard", { claim_id: "cl-1", text: "Chunks are written before the embed." }),
          () => undefined,
        )}
      </SurfaceProvider>,
    );
    expect(view.container.textContent).toContain("Chunks are written before the embed.");
  });
});

describe("useSurface", () => {
  it("carries the project and surface the tree was mounted for", () => {
    let seen: { projectId: string; surface: string; paneKind: string } | null = null;
    function Probe() {
      seen = useSurface();
      return null;
    }
    render(
      <SurfaceProvider projectId="proj-9" surface="Notes">
        <Probe />
      </SurfaceProvider>,
    );
    expect(seen).toEqual({ projectId: "proj-9", surface: "Notes", paneKind: "" });
  });

  it("carries the pane kind, so a surface can open another of its own kind", () => {
    // Without this, `SettingsSurface`'s "Open" button had to name its own pane
    // — `openPane("Settings")` — which is a client-side surface name, the one
    // thing `GET /panes` exists to abolish.
    let seen = "";
    function Probe() {
      seen = useSurface().paneKind;
      return null;
    }
    render(
      <SurfaceProvider projectId="p" surface="disputeQueueSurface" paneKind="dispute-queue">
        <Probe />
      </SurfaceProvider>,
    );
    expect(seen).toBe("dispute-queue");
  });

  it("refuses to be used outside a SurfaceProvider", () => {
    // Returning a default would give every card `projectId: ""`, which fetches
    // nothing and renders an empty card — a failure with no error anywhere.
    function Orphan() {
      useSurface();
      return null;
    }
    expect(() => render(<Orphan />)).toThrow(/SurfaceProvider/);
  });
});

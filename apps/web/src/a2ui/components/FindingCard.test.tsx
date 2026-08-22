/**
 * A finding's evidence, which the reviewer never saw.
 *
 * `routes/surfaces.py` reads `ReviewFinding.evidence_refs_jsonb`,
 * `finding_card()` sends it, `catalog.json` declares it, the client zod schema
 * declares it, and the A2UI binder resolved it and handed it to this view —
 * which did not destructure the prop. Every direction of the prop contract was
 * satisfied and the value still arrived nowhere: the reviewer was asked to
 * approve or dismiss a finding with only a one-line summary under it.
 *
 * `scripts/check-surface-bindings.sh` now has the zod → view direction that
 * catches the class. This is the reader that closes this instance of it.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { FindingCard } from "@/a2ui/components/FindingCard";
import { SurfaceProvider } from "@/a2ui/surface-context";
import type { A2UIComponent } from "@/a2ui/catalog";

const FINDING_ID = "55555555-5555-4555-8555-555555555555";

function mount(props: Record<string, unknown>) {
  const component = { type: "FindingCard", id: "f", props } as A2UIComponent;
  return render(
    <SurfaceProvider projectId="p" surface="briefs">
      <FindingCard component={component} onAction={() => undefined} />
    </SurfaceProvider>,
  );
}

const BASE = {
  finding_id: FINDING_ID,
  severity: "high",
  kind: "unsupported_claim",
  summary: "This claim has no citation.",
};

describe("FindingCard evidence", () => {
  it("renders every evidence ref the producer sent", () => {
    mount({
      ...BASE,
      evidence_refs: [
        { kind: "claim", id: "c-1", label: "Claim about latency" },
        { kind: "source", id: "s-2" },
      ],
    });
    const list = screen.getByTestId("finding-evidence-refs");
    expect(list.textContent).toContain("Claim about latency");
    // No label on the second ref, so the id is what identifies it — falling
    // back to nothing would render a bullet with a bare "source:" beside it.
    expect(list.textContent).toContain("s-2");
    expect(list.querySelectorAll("li")).toHaveLength(2);
  });

  it("renders no evidence list at all when the finding has none", () => {
    // The empty case must not leave an empty bullet list on screen: a finding
    // with no evidence and a finding whose evidence was dropped have to look
    // different, which is the whole reason this prop is worth reading.
    mount({ ...BASE, evidence_refs: [] });
    expect(screen.queryByTestId("finding-evidence-refs")).toBeNull();
  });

  it("survives a producer that omits the prop entirely", () => {
    mount(BASE);
    expect(screen.queryByTestId("finding-evidence-refs")).toBeNull();
    expect(screen.getByText("This claim has no citation.")).toBeTruthy();
  });
});

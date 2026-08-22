/**
 * A wikilink is a claim about the corpus, and a broken one must look broken.
 *
 * Two rules, both of which fail silently if they regress:
 *
 *   - **the pipe form navigates to the TARGET, not the display text.** The
 *     generated hubs emit `[[logging-recovery-hub|Logging and Recovery Hub]]`,
 *     and navigating to the display half looks up a page filed under no such
 *     name — a 404 that reads as a missing page rather than a broken link.
 *   - **an unresolved link is not clickable.** Rendered as an ordinary chip it
 *     invites a click that goes nowhere, which is indistinguishable from a slow
 *     app.
 */
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WikilinkChip } from "@/components/WikilinkChip";

describe("WikilinkChip", () => {
  it("navigates to the link text for the plain [[title]] form", () => {
    const onNavigate = vi.fn();
    const view = render(<WikilinkChip text="Distillation" onNavigate={onNavigate} />);
    fireEvent.click(view.getByTestId("wikilink-chip"));
    expect(onNavigate).toHaveBeenCalledWith("Distillation");
  });

  it("navigates to the target, not the display half, for [[target|display]]", () => {
    const onNavigate = vi.fn();
    const view = render(
      <WikilinkChip
        text="Logging and Recovery Hub"
        target="logging-recovery-hub"
        onNavigate={onNavigate}
      />,
    );
    fireEvent.click(view.getByTestId("wikilink-chip"));
    expect(onNavigate).toHaveBeenCalledWith("logging-recovery-hub");
  });

  it("shows the display text, whichever form it is", () => {
    const view = render(<WikilinkChip text="Logging and Recovery Hub" target="logging-recovery-hub" />);
    expect(view.getByTestId("wikilink-chip").textContent).toBe("Logging and Recovery Hub");
  });

  it("renders a broken link as a distinct, non-clickable state", () => {
    const onNavigate = vi.fn();
    const view = render(<WikilinkChip text="Missing Page" broken onNavigate={onNavigate} />);
    const broken = view.getByTestId("wikilink-broken");
    expect(broken.textContent).toBe("[[Missing Page]]");
    expect(view.queryByTestId("wikilink-chip")).toBeNull();
    fireEvent.click(broken);
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it("says in its tooltip which title failed to resolve", () => {
    const view = render(<WikilinkChip text="Methods" target="methods-v2" broken />);
    expect(view.getByTestId("wikilink-broken").getAttribute("title")).toContain("methods-v2");
  });

  it("does not throw when there is no navigation handler", () => {
    const view = render(<WikilinkChip text="Distillation" />);
    expect(() => fireEvent.click(view.getByTestId("wikilink-chip"))).not.toThrow();
  });
});

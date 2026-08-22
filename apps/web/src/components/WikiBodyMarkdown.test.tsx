/**
 * The wiki body renderer — the last hop between stored markdown and the reader.
 *
 * Its inline tokeniser is one alternating regex over four forms, and every one
 * of them fails QUIETLY when it is wrong: a mis-parsed `[[target|display]]`
 * renders as a chip that looks clickable and navigates to a title nothing is
 * filed under, and a mis-parsed `[cN]` renders the raw marker in the middle of
 * a sentence. Neither raises, and both look like a content problem rather than
 * a rendering one.
 */
import { fireEvent, render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WikiBodyMarkdown } from "@/components/WikiBodyMarkdown";

describe("WikiBodyMarkdown", () => {
  it("renders headings and paragraphs as separate blocks", () => {
    const view = render(<WikiBodyMarkdown body={"# Title\n\nFirst para.\n\nSecond para."} />);
    expect(view.container.querySelector("h1")?.textContent).toBe("Title");
    expect(view.container.querySelectorAll("p")).toHaveLength(2);
  });

  it("splits [[target|display]] into a chip that SHOWS the display half", () => {
    const view = render(<WikiBodyMarkdown body="See [[logging-recovery-hub|Logging and Recovery Hub]]." />);
    expect(view.getByTestId("wikilink-chip").textContent).toBe("Logging and Recovery Hub");
  });

  it("navigates by the TARGET half, which is the only name the corpus is filed under", () => {
    // Without the split, the whole string — pipe included — is looked up as a
    // title, matches nothing, and every link on every generated hub renders as
    // broken raw text.
    const onNavigate = vi.fn();
    const view = render(
      <WikiBodyMarkdown
        body="See [[logging-recovery-hub|Logging and Recovery Hub]]."
        onNavigate={onNavigate}
      />,
    );
    fireEvent.click(view.getByTestId("wikilink-chip"));
    expect(onNavigate).toHaveBeenCalledWith("logging-recovery-hub", "Logging and Recovery Hub");
  });

  it("strips a section anchor, because [[page#section]] addresses the same page", () => {
    const onNavigate = vi.fn();
    const view = render(
      <WikiBodyMarkdown body="See [[Distillation#Methods]]." onNavigate={onNavigate} />,
    );
    fireEvent.click(view.getByTestId("wikilink-chip"));
    expect(onNavigate).toHaveBeenCalledWith("Distillation", "Distillation");
  });

  it("marks a link the resolver rejects as broken, and asks about the target", () => {
    const resolveLink = vi.fn().mockReturnValue(false);
    const view = render(
      <WikiBodyMarkdown body="See [[Missing Page]]." resolveLink={resolveLink} />,
    );
    expect(view.getByTestId("wikilink-broken")).toBeTruthy();
    expect(resolveLink).toHaveBeenCalledWith("Missing Page", "Missing Page");
  });

  it("treats an unknown resolution as clickable rather than broken", () => {
    // `null` means "not looked up", which is not the same as "does not exist".
    // Rendering unknown as broken makes an ordinary page look like a corpus
    // full of dead links.
    const view = render(
      <WikiBodyMarkdown body="See [[Distillation]]." resolveLink={() => null} />,
    );
    expect(view.getByTestId("wikilink-chip")).toBeTruthy();
    expect(view.queryByTestId("wikilink-broken")).toBeNull();
  });

  it("renders [text](url) and a bare URL as external links that cannot reach the opener", () => {
    const view = render(
      <WikiBodyMarkdown body="See [the paper](https://example.org/a) and https://example.org/b" />,
    );
    const links = view.getAllByTestId("wiki-external-link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual([
      "https://example.org/a",
      "https://example.org/b",
    ]);
    // `rel=noreferrer` on a `target=_blank` link is what stops the opened page
    // reaching back through `window.opener`.
    expect(links[0].getAttribute("rel")).toBe("noreferrer");
  });

  it("hands a [cN] marker to the caller's citation renderer", () => {
    const renderCitation = vi.fn((marker: string) => <span data-testid="cite">{marker}</span>);
    const view = render(
      <WikiBodyMarkdown body="Chunks are written first [c3]." renderCitation={renderCitation} />,
    );
    expect(renderCitation).toHaveBeenCalledWith("c3");
    expect(view.getByTestId("cite").textContent).toBe("c3");
  });

  it("keeps the surrounding prose around every token", () => {
    // The tokeniser walks with a cursor; an off-by-one drops the text between
    // two matches, which reads as a page that lost half its sentences.
    const view = render(
      <WikiBodyMarkdown body="Before [[A]] middle [[B]] after." resolveLink={() => null} />,
    );
    expect(view.container.textContent).toBe("Before A middle B after.");
  });
});

/**
 * `stripFrontmatter` decides what the reader sees at the top of a page.
 *
 * The rule that matters is the one that is easy to get wrong: the block is the
 * region between the FIRST two `---` fences, and only when the document opens
 * with one. A `---` further down is a horizontal rule, and a greedy match eats
 * the page body — which fails silently, because a page with its body removed
 * still renders.
 */
import { describe, expect, it } from "vitest";

import { stripFrontmatter } from "@/lib/frontmatter";

describe("stripFrontmatter", () => {
  it("removes a leading frontmatter block", () => {
    const md = "---\ntitle: Logging Hub\ntype: hub\n---\n# Logging Hub\n\nBody.";
    expect(stripFrontmatter(md)).toBe("# Logging Hub\n\nBody.");
  });

  it("leaves a document with no frontmatter untouched", () => {
    const md = "# Logging Hub\n\nBody.";
    expect(stripFrontmatter(md)).toBe(md);
  });

  it("does not treat a horizontal rule mid-document as a fence", () => {
    // The defect this guards: a greedy or unanchored match starts at the `---`
    // on line 3 and deletes everything up to the next one, so the page loses
    // its body and still renders as a valid, shorter page.
    const md = "# Title\n\n---\n\nSection two.\n\n---\n\nSection three.";
    expect(stripFrontmatter(md)).toBe(md);
  });

  it("strips a CRLF frontmatter block", () => {
    const md = "---\r\ntitle: X\r\n---\r\nBody.";
    expect(stripFrontmatter(md)).toBe("Body.");
  });

  it("strips only the first block, not a second fence pair further down", () => {
    const md = "---\ntitle: X\n---\nBody.\n\n---\nnot frontmatter\n---\n";
    expect(stripFrontmatter(md)).toBe("Body.\n\n---\nnot frontmatter\n---\n");
  });
});

/**
 * Strip a YAML frontmatter block from a markdown body.
 *
 * Mirrors `aleph_wiki.frontmatter` on the server, including the rule that only
 * matters once you get it wrong: the block is the region between the FIRST two
 * `---` fences, and only when the document opens with one. A `---` further down
 * is a horizontal rule, and matching it would silently eat the page's body.
 *
 * The reader strips rather than renders because the fields are already on the
 * page as badges and headings — showing them again as literal text
 * (`---title: Logging and Recovery Hub type: hub ---`) is the raw storage
 * format leaking into the reading surface.
 */
const FENCE = /^---[ \t]*\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/;

export function stripFrontmatter(markdown: string): string {
  return markdown.replace(FENCE, "");
}

import { useMemo, type JSX } from "react";

import { WikilinkChip } from "@/components/WikilinkChip";

interface Props {
  body: string;
  onNavigate?: (target: string, label?: string) => void;
  /**
   * Resolution state for an inline `[[wikilink]]` by its title:
   * `true` = resolves to a page, `false` = broken/unresolved, `null`/undefined
   * = unknown (render as a normal clickable chip). Lets the body renderer mark
   * broken links distinctly without re-fetching.
   */
  resolveLink?: (target: string, label?: string) => boolean | null;
  /**
   * Optional custom renderer for a `[cN]` citation marker. When provided (the
   * WP-4b reader tier), it renders the interactive citation badge/popover;
   * otherwise `[cN]` falls back to a static badge.
   */
  renderCitation?: (marker: string) => JSX.Element | null;
}

// Inline token matcher, alternation tried left-to-right at each position:
//   1: [[wikilink]]          2/3: [text](url)        4: [cN] citation
//   5: bare http(s) URL
const TOKEN_RE =
  /\[\[([^\]]+)\]\]|\[([^\]]+)\]\(([^)\s]+)\)|\[(c\d+)\]|(https?:\/\/[^\s)<>]+)/g;

/**
 * Lightweight Markdown renderer for wiki bodies. Headings render as styled
 * `<h*>`; paragraphs as `<p>`. Inline: `[[wikilink]]` becomes a navigable chip
 * (broken ones styled distinct), `[text](url)` and bare URLs become external
 * anchors, `[cN]` becomes a citation badge.
 */
export function WikiBodyMarkdown({ body, onNavigate, resolveLink, renderCitation }: Props) {
  const blocks = useMemo(() => splitBlocks(body), [body]);
  return (
    <div className="prose prose-slate max-w-none break-words text-sm">
      {blocks.map((block, i) => renderBlock(block, i, onNavigate, resolveLink, renderCitation))}
    </div>
  );
}

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "para"; text: string };

function splitBlocks(body: string): Block[] {
  const out: Block[] = [];
  for (const raw of body.split(/\n{2,}/)) {
    const trimmed = raw.trim();
    if (!trimmed) continue;
    const hm = /^(#{1,6})\s+(.+)$/.exec(trimmed);
    if (hm) {
      out.push({ kind: "heading", level: hm[1].length, text: hm[2] });
    } else {
      out.push({ kind: "para", text: trimmed });
    }
  }
  return out;
}

function ExternalLink({ href, label, k }: { href: string; label: string; k: string }) {
  return (
    <a
      key={k}
      href={href}
      target="_blank"
      rel="noreferrer"
      className="break-words text-accent underline decoration-line-strong underline-offset-2 hover:decoration-accent"
      data-testid="wiki-external-link"
    >
      {label}
    </a>
  );
}

function renderInline(
  text: string,
  onNavigate?: (target: string, label?: string) => void,
  resolveLink?: (target: string, label?: string) => boolean | null,
  renderCitation?: (marker: string) => JSX.Element | null,
): Array<string | JSX.Element> {
  const out: Array<string | JSX.Element> = [];
  let cursor = 0;
  let k = 0;
  TOKEN_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = TOKEN_RE.exec(text)) !== null) {
    const start = m.index;
    if (start > cursor) out.push(text.slice(cursor, start));
    if (m[1] !== undefined) {
      // Obsidian's `[[target|display]]`. The pipe form is what the generated
      // hubs emit (`[[logging-recovery-hub|Logging and Recovery Hub]]`), and
      // without splitting it the whole string — pipe included — is looked up as
      // a title, matches nothing, and every link on every hub renders as broken
      // raw text. Anchors (`[[target#section]]`) address the same page.
      const raw = m[1];
      const pipe = raw.indexOf("|");
      const target = (pipe === -1 ? raw : raw.slice(0, pipe)).split("#")[0].trim();
      const label = pipe === -1 ? target : raw.slice(pipe + 1).trim() || target;
      const resolved = resolveLink ? resolveLink(target, label) : null;
      out.push(
        <WikilinkChip
          key={`w-${k}`}
          text={label}
          target={target}
          onNavigate={(t) => onNavigate?.(t, label)}
          broken={resolved === false}
        />,
      );
    } else if (m[2] !== undefined && m[3] !== undefined) {
      out.push(<ExternalLink key={`l-${k}`} k={`l-${k}`} href={m[3]} label={m[2]} />);
    } else if (m[4] !== undefined) {
      const custom = renderCitation ? renderCitation(m[4]) : null;
      out.push(
        custom ?? (
          <span
            key={`c-${k}`}
            title={m[4]}
            className="mx-0.5 inline-flex items-baseline bg-badge-warning-bg px-1 text-[10px] font-semibold uppercase tracking-wider text-badge-warning-fg"
          >
            {m[4]}
          </span>
        ),
      );
    } else if (m[5] !== undefined) {
      out.push(<ExternalLink key={`u-${k}`} k={`u-${k}`} href={m[5]} label={m[5]} />);
    }
    cursor = start + m[0].length;
    k++;
  }
  if (cursor < text.length) out.push(text.slice(cursor));
  return out;
}

function renderBlock(
  block: Block,
  i: number,
  onNavigate?: (target: string, label?: string) => void,
  resolveLink?: (target: string, label?: string) => boolean | null,
  renderCitation?: (marker: string) => JSX.Element | null,
) {
  if (block.kind === "heading") {
    const cls =
      block.level === 1
        ? "mt-6 mb-3 text-2xl font-semibold"
        : block.level === 2
          ? "mt-5 mb-2 text-lg font-semibold"
          : "mt-4 mb-2 text-base font-semibold";
    const Tag = `h${block.level}` as keyof JSX.IntrinsicElements;
    return (
      <Tag key={i} className={cls}>
        {renderInline(block.text, onNavigate, resolveLink, renderCitation)}
      </Tag>
    );
  }
  return (
    <p key={i} className="my-3 leading-relaxed text-ink-soft">
      {renderInline(block.text, onNavigate, resolveLink, renderCitation)}
    </p>
  );
}

import { useMemo, type JSX } from "react";

import { WikilinkChip } from "@/components/WikilinkChip";

interface Props {
  body: string;
  onNavigate?: (target: string) => void;
}

const WIKILINK_RE = /\[\[([^\]]+)\]\]/g;
const CITATION_RE = /\[(c\d+)\]/g;
const HEADING_RE = /^(#{1,6})\s+(.+)$/gm;

/**
 * Lightweight Markdown renderer for wiki bodies. Headings render as styled
 * `<h*>`; paragraphs as `<p>`; `[[wikilink]]` becomes a chip; `[cN]` becomes
 * a tooltip-anchored span (Inc 2 will resolve the chunk preview content).
 *
 * This is intentionally minimal — A2UI WikiSurface in Inc 4 replaces this.
 */
export function WikiBodyMarkdown({ body, onNavigate }: Props) {
  const blocks = useMemo(() => splitBlocks(body), [body]);
  return (
    <div className="prose prose-slate max-w-none text-sm">
      {blocks.map((block, i) => renderBlock(block, i, onNavigate))}
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
    HEADING_RE.lastIndex = 0;
    const hm = /^(#{1,6})\s+(.+)$/.exec(trimmed);
    if (hm) {
      out.push({ kind: "heading", level: hm[1].length, text: hm[2] });
    } else {
      out.push({ kind: "para", text: trimmed });
    }
  }
  return out;
}

function renderInline(text: string, onNavigate?: (target: string) => void) {
  const parts: Array<string | JSX.Element> = [];
  let cursor = 0;
  WIKILINK_RE.lastIndex = 0;
  for (const m of text.matchAll(WIKILINK_RE)) {
    const start = m.index ?? 0;
    if (start > cursor) parts.push(text.slice(cursor, start));
    const inner = m[1];
    parts.push(
      <WikilinkChip key={`w-${start}`} text={inner} onNavigate={onNavigate} />,
    );
    cursor = start + m[0].length;
  }
  if (cursor < text.length) parts.push(text.slice(cursor));

  return parts.map((p, idx) => {
    if (typeof p !== "string") return p;
    const inner: Array<string | JSX.Element> = [];
    let c = 0;
    CITATION_RE.lastIndex = 0;
    for (const cm of p.matchAll(CITATION_RE)) {
      const cs = cm.index ?? 0;
      if (cs > c) inner.push(p.slice(c, cs));
      inner.push(
        <span
          key={`c-${idx}-${cs}`}
          title={cm[1]}
          className="mx-0.5 inline-flex items-baseline rounded bg-amber-100 px-1 text-[10px] font-semibold uppercase tracking-wider text-amber-900"
        >
          {cm[1]}
        </span>,
      );
      c = cs + cm[0].length;
    }
    if (c < p.length) inner.push(p.slice(c));
    return inner.length === 1 && typeof inner[0] === "string" ? p : <span key={idx}>{inner}</span>;
  });
}

function renderBlock(
  block: Block,
  i: number,
  onNavigate?: (target: string) => void,
) {
  if (block.kind === "heading") {
    const cls = block.level === 1
      ? "mt-6 mb-3 text-2xl font-semibold"
      : block.level === 2
        ? "mt-5 mb-2 text-lg font-semibold"
        : "mt-4 mb-2 text-base font-semibold";
    const Tag = `h${block.level}` as keyof JSX.IntrinsicElements;
    return (
      <Tag key={i} className={cls}>
        {renderInline(block.text, onNavigate)}
      </Tag>
    );
  }
  return (
    <p key={i} className="my-3 leading-relaxed text-slate-700">
      {renderInline(block.text, onNavigate)}
    </p>
  );
}

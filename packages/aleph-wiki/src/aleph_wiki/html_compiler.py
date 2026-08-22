"""Deterministic, non-LLM wiki-page → HTML compiler (WP-4 sub-spec b).

`compile_page_html` turns a wiki page's markdown body + claims + optional
infobox metadata into a single self-contained styled HTML document:

* **Deterministic** — a pure function of its inputs. The same inputs produce
  byte-identical output (property-testable): no timestamps, no random ids, no
  environment lookups, stable iteration order (claims in list order, infobox in
  dict-insertion order).
* **Self-contained** — inline ``<style>`` only. NO ``<script>``, no external
  URLs / CDNs / fonts / images. Safe to serve inside a sandboxed iframe.
* **Escaped** — every piece of user/agent content is HTML-escaped. The markdown
  body is rendered by markdown-it-py with raw-HTML disabled, so a body can never
  inject markup.

Markdown remains the only wiki write-format; this compiler *reads* markdown and
metadata and never writes either back.
"""

from __future__ import annotations

from html import escape
from typing import Any

from aleph_core.confidence import Confidence, canonical_confidence

#: Badge colours, by state. Keyed on the enum so adding a member without a
#: colour is a KeyError at import — loud, and at the moment the state is added
#: rather than the first time a claim reaches it.
_CONF_STYLE: dict[Confidence, str] = {
    Confidence.UNDER_INVESTIGATION: "background:#f1f5f9;color:#475569",
    Confidence.WEAKLY_SUPPORTED: "background:#fef3c7;color:#854d0e",
    Confidence.WELL_SUPPORTED: "background:#d1fae5;color:#065f46",
    Confidence.CONTESTED: "background:#ffedd5;color:#9a3412",
    Confidence.REFUTED: "background:#fee2e2;color:#991b1b",
    Confidence.ABANDONED: "background:#e2e8f0;color:#334155",
}

# System font stack only — no @font-face, no external font URLs.
_FONT_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

_STYLE = (
    "*{box-sizing:border-box}"
    "body{margin:0;padding:2rem 2.5rem;font-family:" + _FONT_STACK + ";"
    "font-size:15px;line-height:1.6;color:#0f172a;background:#ffffff;"
    "max-width:52rem;margin-left:auto;margin-right:auto}"
    "h1{font-size:1.9rem;font-weight:700;margin:0 0 1rem;line-height:1.2}"
    "h2{font-size:1.4rem;font-weight:600;margin:2rem 0 .75rem}"
    "h3{font-size:1.15rem;font-weight:600;margin:1.5rem 0 .5rem}"
    "p{margin:0 0 1rem}"
    "a{color:#0369a1;text-decoration:underline}"
    "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9em;"
    "background:#f1f5f9;padding:.1em .35em;border-radius:.25rem}"
    "pre{background:#f1f5f9;padding:1rem;border-radius:.5rem;overflow:auto}"
    "pre code{background:none;padding:0}"
    "table{border-collapse:collapse;width:100%;margin:1rem 0}"
    "th,td{border:1px solid #cbd5e1;padding:.4rem .6rem;text-align:left}"
    "th{background:#f8fafc}"
    ".infobox{float:right;width:18rem;margin:0 0 1rem 1.5rem;border:1px solid #e2e8f0;"
    "border-radius:.5rem;background:#f8fafc;font-size:.85rem}"
    ".infobox caption{font-weight:600;padding:.5rem;background:#f1f5f9;"
    "border-bottom:1px solid #e2e8f0;border-radius:.5rem .5rem 0 0}"
    ".infobox th{width:40%;background:none;font-weight:600;vertical-align:top}"
    ".claims{margin-top:2.5rem;border-top:1px solid #e2e8f0;padding-top:1rem}"
    ".claims h2{margin-top:0}"
    ".claim{margin:0 0 .75rem;padding:.6rem .8rem;border:1px solid #e2e8f0;"
    "border-radius:.5rem}"
    ".claim .conf{display:inline-block;font-size:.7rem;font-weight:700;"
    "text-transform:uppercase;letter-spacing:.04em;padding:.1rem .4rem;"
    "border-radius:.25rem;margin-bottom:.3rem}"
    # One class per member of `aleph_core.confidence.Confidence`, generated
    # from the enum rather than typed out, so a new state cannot arrive with no
    # styling. Three of the six used to have no rule at all — refuted,
    # abandoned and under_investigation all fell through to `.conf-default`,
    # which is how a claim the evidence had DISPROVED rendered in the same grey
    # as one nobody had looked at yet.
    + "".join(f".conf-{c.value}{{{_CONF_STYLE[c]}}}" for c in Confidence)
    + ".conf-unknown{background:#f1f5f9;color:#475569}"
)


def _conf_class(confidence: str) -> str:
    """CSS class for a confidence value.

    A value outside the vocabulary gets `conf-unknown` and is rendered as
    itself, not silently relabelled: the compiler is a READER of whatever the
    column holds, and a page that quietly showed "under investigation" for a
    word it did not recognise would hide the drift this workstream exists to
    remove. `canonical_confidence` translates the known legacy spellings only.
    """
    try:
        return f"conf-{canonical_confidence(confidence).value}"
    except ValueError:
        return "conf-unknown"


def _markdown_to_html(md: str) -> str:
    """Render markdown to HTML deterministically with raw HTML disabled.

    Mirrors the exporters/pdf.py pattern (markdown-it-py, table extension) but
    forces ``html=False`` so a body can never smuggle raw markup into the
    sandboxed document.
    """
    from markdown_it import MarkdownIt

    md_render = MarkdownIt("commonmark", {"html": False, "linkify": False})
    md_render.enable("table")
    return md_render.render(md)


def _render_infobox(title: str, infobox: dict[str, Any] | None) -> str:
    if not infobox:
        return ""
    rows: list[str] = []
    # Dict insertion order is preserved (py3.7+), so output is deterministic.
    for key, value in infobox.items():
        rows.append(f"<tr><th>{escape(str(key))}</th><td>{escape(str(value))}</td></tr>")
    return (
        f'<table class="infobox"><caption>{escape(title)}</caption>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_claims(claims: list[dict[str, Any]]) -> str:
    if not claims:
        return ""
    items: list[str] = []
    for claim in claims:
        confidence = str(claim.get("confidence", "") or "")
        text = str(claim.get("text", "") or "")
        # An empty confidence is not "uncited" — it is a row nobody has
        # derived yet, and the vocabulary has a word for that.
        conf_label = escape(confidence) if confidence else Confidence.UNDER_INVESTIGATION.value
        items.append(
            f'<div class="claim">'
            f'<span class="conf {_conf_class(confidence)}">{conf_label}</span>'
            f"<div>{escape(text)}</div>"
            f"</div>"
        )
    return f'<section class="claims"><h2>Claims</h2>{"".join(items)}</section>'


def compile_page_html(
    *,
    title: str,
    body_md: str,
    claims: list[dict[str, Any]] | None = None,
    infobox: dict[str, Any] | None = None,
) -> str:
    """Compile a wiki page into a self-contained, styled HTML document.

    Deterministic: identical ``(title, body_md, claims, infobox)`` inputs yield
    byte-identical output. ``claims`` is a list of ``{text, confidence}`` dicts;
    ``infobox`` is optional key/value metadata rendered as a table. No scripts,
    no external references — safe for a sandboxed iframe.
    """
    claims = claims or []
    safe_title = escape(title)
    body_html = _markdown_to_html(body_md)
    infobox_html = _render_infobox(title, infobox)
    claims_html = _render_claims(claims)
    return (
        "<!doctype html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{safe_title}</title>"
        f"<style>{_STYLE}</style>"
        "</head><body>"
        f"{infobox_html}"
        f"<h1>{safe_title}</h1>"
        f"{body_html}"
        f"{claims_html}"
        "</body></html>"
    )

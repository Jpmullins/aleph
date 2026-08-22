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
* **Themed** — the document carries BOTH of Aleph's palettes and switches on
  ``prefers-color-scheme``. It used to be a fixed sheet of Tailwind-default
  slate on ``#ffffff``: opened from the dark workspace it was a white rectangle
  in the middle of a near-black page, and it was the one reading surface the
  design system did not reach. `docs/plan.md` WS-E3.

Markdown remains the only wiki write-format; this compiler *reads* markdown and
metadata and never writes either back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from typing import Any

from aleph_core.confidence import Confidence, canonical_confidence

#: Badge ROLE per confidence state, keyed on the enum so adding a member
#: without a role is a KeyError at import — loud, and at the moment the state
#: is added rather than the first time a claim reaches it.
#:
#: The values used to be literal CSS (`"background:#f1f5f9;color:#475569"`),
#: which is why this file held a second, divergent palette: six background/
#: foreground pairs invented here, tuned against a white page, with no dark
#: counterpart and no relationship to `apps/web/src/styles/tokens.css`. A role
#: name resolves to whichever pair the ACTIVE palette declares, so both themes
#: are covered by construction and there is one place a colour is decided.
#:
#: Five roles for six states, deliberately. `abandoned` and
#: `under_investigation` are both "nobody is currently arguing this" and share
#: the idle grey; they stay distinguishable because `.conf-abandoned` is struck
#: through, which is a stronger signal than a 3% shift in grey and survives
#: both themes and a monochrome print.
_CONF_STYLE: dict[Confidence, str] = {
    Confidence.UNDER_INVESTIGATION: "idle",
    Confidence.WEAKLY_SUPPORTED: "warning",
    Confidence.WELL_SUPPORTED: "completed",
    Confidence.CONTESTED: "running",
    Confidence.REFUTED: "failed",
    Confidence.ABANDONED: "idle",
}

#: The five badge roles `apps/web/src/styles/tokens.css` defines a bg/fg pair
#: for. Ordered, because the CSS is emitted from it and the output must be
#: byte-stable.
_BADGE_ROLES: tuple[str, ...] = ("idle", "running", "completed", "failed", "warning")

# The three faces, spelled exactly as `--font-ui` / `--font-mono` /
# `--font-prose` in tokens.css, so `test_html_compiler.py` can assert the two
# files agree character for character.
#
# No `@font-face` and no external URL: the compiled document is served with
# `Content-Security-Policy: sandbox`, so it has an opaque origin and could not
# fetch the app's vendored woff2 files even if it named them. Naming the
# families first still costs nothing and wins on any machine that has them
# installed; every stack ends in a real system fallback.
_FONT_UI = '"Public Sans", ui-sans-serif, system-ui, sans-serif'
_FONT_MONO = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace'
_FONT_PROSE = '"Newsreader", Georgia, "Times New Roman", serif'


@dataclass(frozen=True)
class DocPalette:
    """One theme's worth of colour, mirroring a block of `tokens.css`.

    Field names are the token names with the `--` dropped and dashes turned to
    underscores, so `surface_bg` is `--surface-bg` and a reader can grep one
    name across both files. `packages/aleph-wiki/tests/test_html_compiler.py`
    asserts every value here equals the value the stylesheet declares — this
    file is a mirror, and an unchecked mirror is just a second source of truth.
    """

    name: str
    surface_bg: str
    surface_raised: str
    surface_sunken: str
    border_muted: str
    border_strong: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent: str
    #: role → (background, foreground). Keys are exactly `_BADGE_ROLES`.
    badges: Mapping[str, tuple[str, str]]


LIGHT = DocPalette(
    name="light",
    surface_bg="#FBFAF8",
    surface_raised="#FFFFFF",
    surface_sunken="#F3F2EF",
    border_muted="#E2E0DB",
    border_strong="#C9C6BF",
    text_primary="#14171A",
    text_secondary="#565C61",
    text_muted="#868C91",
    accent="#3A342B",
    badges={
        "idle": ("#F3F2EF", "#565C61"),
        "running": ("#EDEAE2", "#3A342B"),
        "completed": ("#E4EDE7", "#2E6B4A"),
        "failed": ("#F6E7E3", "#93412F"),
        "warning": ("#F0EADC", "#6B5A2E"),
    },
)

DARK = DocPalette(
    name="dark",
    surface_bg="#0C0E10",
    surface_raised="#14171A",
    surface_sunken="#1B1F23",
    border_muted="#24282C",
    border_strong="#333940",
    text_primary="#E8EAEC",
    text_secondary="#A0A8AE",
    text_muted="#6B747B",
    accent="#D9D4C8",
    badges={
        "idle": ("#1B1F23", "#A0A8AE"),
        "running": ("#2A2721", "#D9D4C8"),
        "completed": ("#16261F", "#7FA98F"),
        "failed": ("#2A1917", "#C08478"),
        "warning": ("#2A2721", "#C9BE9E"),
    },
)


def _custom_properties(palette: DocPalette) -> str:
    """One palette as `--doc-*` declarations, in a fixed order.

    Prefixed `--doc-` rather than reusing the app's `--surface-bg` names: the
    document is a separate one, and a name collision between an embedded page
    and its host is the kind of thing that works until someone stops using an
    iframe.
    """
    decls = [
        f"--doc-bg:{palette.surface_bg}",
        f"--doc-raised:{palette.surface_raised}",
        f"--doc-sunken:{palette.surface_sunken}",
        f"--doc-line:{palette.border_muted}",
        f"--doc-line-strong:{palette.border_strong}",
        f"--doc-ink:{palette.text_primary}",
        f"--doc-ink-soft:{palette.text_secondary}",
        f"--doc-ink-muted:{palette.text_muted}",
        f"--doc-accent:{palette.accent}",
    ]
    for role in _BADGE_ROLES:
        background, foreground = palette.badges[role]
        decls.append(f"--doc-{role}-bg:{background}")
        decls.append(f"--doc-{role}-fg:{foreground}")
    return ";".join(decls) + ";"


# No `border-radius` anywhere in this sheet, and that is a design decision, not
# an omission: tokens.css pins `--radius: 0px` and the whole interface is
# squared. Four rounded corners here — code, pre, the infobox, the claim card —
# were the only curves anywhere in the product, so the compiled document read
# as a foreign object embedded in the reader. Separation is a hairline border.
_STYLE = (
    # `color-scheme` tells the browser to paint its own furniture — scrollbars,
    # the canvas behind the body, form widgets — to match. Without it a dark
    # document still scrolls with a white scrollbar.
    ":root{color-scheme:light dark;" + _custom_properties(LIGHT) + "}"
    "@media (prefers-color-scheme:dark){:root{" + _custom_properties(DARK) + "}}"
    "*{box-sizing:border-box}"
    # The prose face, because this is a page someone reads at length. The app
    # makes the same split: mono is the instrument, sans is for interface
    # sentences, the serif appears only where there is reading to do.
    "body{margin:0;padding:2rem 2.5rem;font-family:" + _FONT_PROSE + ";"
    "font-size:16px;line-height:1.65;color:var(--doc-ink);background:var(--doc-bg);"
    "max-width:52rem;margin-left:auto;margin-right:auto;"
    "-webkit-font-smoothing:antialiased}"
    "h1,h2,h3{font-family:" + _FONT_UI + ";color:var(--doc-ink)}"
    "h1{font-size:1.9rem;font-weight:700;margin:0 0 1rem;line-height:1.2}"
    "h2{font-size:1.4rem;font-weight:600;margin:2rem 0 .75rem}"
    "h3{font-size:1.15rem;font-weight:600;margin:1.5rem 0 .5rem}"
    "p{margin:0 0 1rem}"
    "a{color:var(--doc-accent);text-decoration:underline}"
    "code{font-family:" + _FONT_MONO + ";font-size:.9em;"
    "background:var(--doc-sunken);color:var(--doc-ink);padding:.1em .35em}"
    "pre{background:var(--doc-sunken);padding:1rem;overflow:auto;"
    "border:1px solid var(--doc-line)}"
    "pre code{background:none;padding:0}"
    "blockquote{margin:0 0 1rem;padding:0 0 0 1rem;color:var(--doc-ink-soft);"
    "border-left:2px solid var(--doc-line-strong)}"
    "hr{border:0;border-top:1px solid var(--doc-line);margin:2rem 0}"
    "table{border-collapse:collapse;width:100%;margin:1rem 0;font-family:" + _FONT_UI + ";"
    "font-size:.9rem}"
    "th,td{border:1px solid var(--doc-line);padding:.4rem .6rem;text-align:left}"
    "th{background:var(--doc-sunken)}"
    ".infobox{float:right;width:18rem;margin:0 0 1rem 1.5rem;"
    "border:1px solid var(--doc-line);background:var(--doc-raised);font-size:.85rem}"
    ".infobox caption{font-weight:600;padding:.5rem;background:var(--doc-sunken);"
    "border-bottom:1px solid var(--doc-line);text-align:left}"
    ".infobox th{width:40%;background:none;font-weight:600;vertical-align:top;"
    "color:var(--doc-ink-soft)}"
    ".claims{margin-top:2.5rem;border-top:1px solid var(--doc-line);padding-top:1rem}"
    ".claims h2{margin-top:0}"
    ".claim{margin:0 0 .75rem;padding:.6rem .8rem;border:1px solid var(--doc-line);"
    "background:var(--doc-raised)}"
    ".claim .conf{display:inline-block;font-family:" + _FONT_MONO + ";font-size:.7rem;"
    "font-weight:700;text-transform:uppercase;letter-spacing:.04em;padding:.1rem .4rem;"
    "margin-bottom:.3rem}"
    # One rule per member of `aleph_core.confidence.Confidence`, generated from
    # the enum rather than typed out, so a new state cannot arrive with no
    # styling. Three of the six used to have no rule at all — refuted,
    # abandoned and under_investigation all fell through to `.conf-default`,
    # which is how a claim the evidence had DISPROVED rendered in the same grey
    # as one nobody had looked at yet.
    + "".join(
        f".conf-{c.value}{{background:var(--doc-{_CONF_STYLE[c]}-bg);"
        f"color:var(--doc-{_CONF_STYLE[c]}-fg)}}"
        for c in Confidence
    )
    # Struck through, not re-coloured: `abandoned` shares the idle grey with
    # `under_investigation` and this is what keeps the two readable apart.
    + ".conf-abandoned{text-decoration:line-through}"
    + ".conf-unknown{background:var(--doc-idle-bg);color:var(--doc-idle-fg)}"
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

    The document is themed for BOTH grounds from one compile. There is no
    ``theme`` argument, and that is deliberate: the render is cached by the
    sha256 of these bytes (`routes/wiki.py::_compile_and_store`), so a
    per-theme compile would double the stored assets and hand the caller a
    parameter it has no way to fill — the iframe is `sandbox=""`, so nothing
    inside it can read the host page's theme. `prefers-color-scheme` is the
    only signal that crosses that boundary, and it is exactly the signal the
    app's own default "system" setting follows.
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

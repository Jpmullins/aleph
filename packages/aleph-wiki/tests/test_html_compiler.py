"""The compiled wiki document: deterministic, self-contained, and themed.

`compile_page_html` is the one reading surface Aleph renders OUTSIDE the React
app — a wiki page turned into standalone HTML, persisted as a `RenderedAsset`
keyed by the sha256 of its bytes and shown in a `sandbox=""` iframe. Three
properties have to hold at once and they pull against each other:

* it must be **byte-deterministic**, or the sha changes on every read and the
  render cache stores a new asset each time;
* it must be **self-contained**, or a sandboxed opaque origin cannot load what
  it references;
* it must **look like Aleph**, which is a moving target held in
  `apps/web/src/styles/tokens.css`.

The last one is why this file reads that stylesheet. The compiler holds a
Python mirror of two token blocks, and a mirror nothing checks is just a second
source of truth that agrees for a while — the exact failure the A2UI catalog
had, and the reason `scripts/check-catalog-generated.sh` exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aleph_core.confidence import Confidence
from aleph_wiki.html_compiler import (
    _BADGE_ROLES,
    _CONF_STYLE,
    _FONT_MONO,
    _FONT_PROSE,
    _FONT_UI,
    DARK,
    LIGHT,
    DocPalette,
    compile_page_html,
)

# packages/aleph-wiki/tests/<this file> → repo root
_ROOT = Path(__file__).resolve().parents[3]
_TOKENS_CSS = _ROOT / "apps/web/src/styles/tokens.css"


def _tokens(selector: str) -> dict[str, str]:
    """Custom properties declared in one block of `tokens.css`.

    Read from disk, by name, and a missing file raises rather than yielding an
    empty dict: every assertion below iterates this, so an empty parse would
    make the whole module pass while checking nothing.
    """
    if not _TOKENS_CSS.exists():
        msg = f"tokens.css not found at {_TOKENS_CSS} — the palette mirror cannot be checked"
        raise FileNotFoundError(msg)
    css = re.sub(r"/\*.*?\*/", "", _TOKENS_CSS.read_text(encoding="utf-8"), flags=re.S)
    start = css.find(selector)
    if start < 0:
        msg = f"tokens.css has no `{selector}` block"
        raise AssertionError(msg)
    body = css[start + len(selector) : css.index("}", start)]
    out = {
        name: value.strip() for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", body)
    }
    if not out:
        msg = f"parsed 0 declarations out of the `{selector}` block"
        raise AssertionError(msg)
    return out


#: The page the theme assertions render. Deliberately not empty: an empty body
#: emits no `<table>`, no `<code>` and no claims, so a sheet that had lost half
#: its rules would still look fine.
_PAGE = {
    "title": "Retrieval never abstains",
    "body_md": (
        "The corpus is **75 sources** and the [index](/wiki/index) is rebuilt nightly.\n\n"
        "> A cosine-distance floor does not fix it.\n\n"
        "```python\nsearch_corpus(query_embedding=None)\n```\n\n"
        "| leg | recall@1 |\n| --- | --- |\n| hybrid | 0.34 |\n| lexical | 0.21 |\n\n"
        "Inline `ChunkHit` and a \u2014 dash, plus \u203a, \u2135, \u2600.\n"
    ),
    "claims": [
        {"text": "Recall@1 is 0.34 on the generated set.", "confidence": "well_supported"},
        {"text": "A distance floor separates the two distributions.", "confidence": "refuted"},
        {"text": "The old 12-document set is saturated.", "confidence": "abandoned"},
        {"text": "Nobody has looked at reranking.", "confidence": ""},
        {"text": "A value the vocabulary does not have.", "confidence": "definitely-maybe"},
    ],
    "infobox": {"Sources": 75, "Chunks": 3451, "Stuck runs": 0},
}


def _style(html: str) -> str:
    match = re.search(r"<style>(.*?)</style>", html, re.S)
    assert match, "the compiled document has no <style> block"
    return match.group(1)


# ---------------------------------------------------------------------------
# Determinism — the render cache keys on the sha256 of these bytes.
# ---------------------------------------------------------------------------


def test_two_calls_with_identical_inputs_produce_identical_bytes() -> None:
    """WS-E3 criterion 4.

    `routes/wiki.py::_compile_and_store` reuses a stored `RenderedAsset` when
    the sha256 of the compiled bytes matches. A compiler that varied — a set
    iterated, a dict of colours ordered by hash, a timestamp — would write a
    new asset row and a new ledger event on every single read of every page,
    and the only symptom would be a table that grows.
    """
    first = compile_page_html(**_PAGE).encode("utf-8")
    second = compile_page_html(**_PAGE).encode("utf-8")
    assert first == second
    # A pure-function claim that never renders anything is easy to pass.
    assert len(first) > 2000


def test_determinism_survives_a_fresh_interpreter() -> None:
    """Ordering that depends on hash randomisation only diverges ACROSS
    processes — `PYTHONHASHSEED` is fixed within one. Two calls in this
    process would agree on a set-ordered stylesheet; two processes would not.
    """
    import os
    import subprocess
    import sys

    script = (
        "import hashlib, json, sys;"
        "from aleph_wiki.html_compiler import compile_page_html;"
        "page = json.loads(sys.stdin.read());"
        "sys.stdout.write(hashlib.sha256(compile_page_html(**page).encode()).hexdigest())"
    )
    import json

    payload = json.dumps(_PAGE)
    digests = {
        subprocess.run(
            [sys.executable, "-c", script],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        ).stdout
        for seed in ("0", "1", "12345")
    }
    assert len(digests) == 1, f"the compiled bytes depend on hash seed: {digests}"


# ---------------------------------------------------------------------------
# Self-contained — the document is served with `Content-Security-Policy:
# sandbox` into a `sandbox=""` iframe, so it has an opaque origin.
# ---------------------------------------------------------------------------


def test_the_document_references_nothing_outside_itself() -> None:
    html = compile_page_html(**_PAGE)
    assert "<script" not in html.lower()
    assert "@font-face" not in html
    assert "url(" not in html
    for marker in ("http://", "https://", "//fonts.", "data:"):
        assert marker not in _style(html), f"the stylesheet reaches out via {marker!r}"


def test_the_document_carries_no_radius() -> None:
    """WS-E3 criterion 5, asserted on the OUTPUT rather than on the source.

    The criterion in the plan is a grep over `html_compiler.py`. That checks
    the literal text of one file; this checks what a reader's browser is
    actually handed, so a radius arriving from a helper, an f-string or a
    future generated block is caught too. Aleph is squared: tokens.css pins
    `--radius: 0px` and the compiled document was the only curved surface left
    in the product.
    """
    assert not re.search(r"border-radius:\s*[^0;]", _style(compile_page_html(**_PAGE)))


# ---------------------------------------------------------------------------
# Themed — the Python palettes are a MIRROR of tokens.css and are checked
# against it. WS-E3 criterion 3.
# ---------------------------------------------------------------------------

_FIELD_TO_TOKEN = {
    "surface_bg": "--surface-bg",
    "surface_raised": "--surface-raised",
    "surface_sunken": "--surface-sunken",
    "border_muted": "--border-muted",
    "border_strong": "--border-strong",
    "text_primary": "--text-primary",
    "text_secondary": "--text-secondary",
    "text_muted": "--text-muted",
    "accent": "--accent",
}


@pytest.mark.parametrize(
    ("palette", "selector"),
    [(LIGHT, ":root {"), (DARK, '[data-theme="dark"] {')],
    ids=["light", "dark"],
)
def test_the_python_palette_mirrors_tokens_css(palette: DocPalette, selector: str) -> None:
    declared = _tokens(selector)
    for field, token in _FIELD_TO_TOKEN.items():
        assert token in declared, f"tokens.css `{selector}` no longer declares {token}"
        assert getattr(palette, field).upper() == declared[token].upper(), (
            f"{palette.name} {field} is {getattr(palette, field)} here and "
            f"{declared[token]} in tokens.css {token}"
        )
    for role in _BADGE_ROLES:
        background, foreground = palette.badges[role]
        assert background.upper() == declared[f"--badge-{role}-bg"].upper()
        assert foreground.upper() == declared[f"--badge-{role}-fg"].upper()


def test_the_font_stacks_are_the_ones_tokens_css_declares() -> None:
    """Character for character, quotes included.

    The compiled document cannot load the app's vendored woff2 files — an
    opaque origin has no way to fetch them — but naming the same families in
    the same order means a machine that has them installed renders the two
    surfaces alike, and a machine that does not falls back the same way. A
    stack that drifts here is a document that reads in a different face from
    the page that opened it, which nothing would report.
    """
    declared = _tokens(":root {")
    assert declared["--font-ui"] == _FONT_UI
    assert declared["--font-mono"] == _FONT_MONO
    assert declared["--font-prose"] == _FONT_PROSE


def test_both_grounds_are_in_the_document_and_dark_is_behind_the_media_query() -> None:
    style = _style(compile_page_html(**_PAGE))
    assert f"--doc-bg:{LIGHT.surface_bg}" in style
    media = re.search(r"@media \(prefers-color-scheme:dark\)\{:root\{(.*?)\}\}", style, re.S)
    assert media, "no prefers-color-scheme block — the document cannot follow the theme"
    assert f"--doc-bg:{DARK.surface_bg}" in media.group(1)
    assert f"--doc-ink:{DARK.text_primary}" in media.group(1)
    # The light ground must NOT be inside the dark block, which is what a
    # copy-paste of the wrong palette looks like.
    assert LIGHT.surface_bg not in media.group(1)
    assert "color-scheme:light dark" in style, (
        "without `color-scheme` the browser paints its own scrollbar and canvas white"
    )


def test_no_colour_in_the_stylesheet_comes_from_outside_the_palettes() -> None:
    """The defect this replaced was one literal: `background:#ffffff`.

    Pinning that one string would not stop the next one, so this asserts the
    whole set — every colour literal the sheet emits has to be a value one of
    the two palettes declares. `#ffffff` is still allowed, because it IS the
    light raised surface; what is no longer allowed is a colour with no theme
    behind it.
    """
    known = {
        value.upper()
        for palette in (LIGHT, DARK)
        for value in (
            *(getattr(palette, field) for field in _FIELD_TO_TOKEN),
            *(c for pair in palette.badges.values() for c in pair),
        )
    }
    found = {
        hexcode.upper()
        for hexcode in re.findall(r"#[0-9a-fA-F]{3,8}\b", _style(compile_page_html(**_PAGE)))
    }
    assert found, "no colour literals at all — the palette is not being emitted"
    assert found <= known, f"colours with no token behind them: {sorted(found - known)}"


@pytest.mark.parametrize("state", list(Confidence), ids=lambda c: c.value)
def test_every_confidence_state_has_a_themed_badge_rule(state: Confidence) -> None:
    style = _style(compile_page_html(**_PAGE))
    role = _CONF_STYLE[state]
    assert f".conf-{state.value}{{background:var(--doc-{role}-bg);" in style
    for palette in (LIGHT, DARK):
        assert role in palette.badges, f"{palette.name} has no `{role}` badge pair"


def test_abandoned_is_distinguishable_from_under_investigation() -> None:
    """They share the idle grey — five badge roles, six states — so the thing
    that tells them apart is not colour. Without this the compiler would be
    back to rendering a dropped line of enquiry identically to one nobody has
    started, which is the failure the badge map was written for.
    """
    style = _style(compile_page_html(**_PAGE))
    assert _CONF_STYLE[Confidence.ABANDONED] == _CONF_STYLE[Confidence.UNDER_INVESTIGATION]
    assert ".conf-abandoned{text-decoration:line-through}" in style


def test_an_unknown_confidence_is_rendered_as_itself() -> None:
    html = compile_page_html(**_PAGE)
    assert 'class="conf conf-unknown"' in html
    assert "definitely-maybe" in html
    assert ".conf-unknown{background:var(--doc-idle-bg)" in _style(html)

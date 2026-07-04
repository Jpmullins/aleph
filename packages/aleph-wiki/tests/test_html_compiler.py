"""compile_page_html — deterministic, escaped, script-free HTML (WP-4b)."""

from __future__ import annotations

from aleph_wiki.html_compiler import compile_page_html

_TITLE = "Attention Mechanisms"
_BODY = (
    "# Overview\n\n"
    "Transformers rely on **attention**. See [[Self-Attention]] and [c1].\n\n"
    "## Details\n\n"
    "- item one\n- item two\n\n"
    "| col a | col b |\n| --- | --- |\n| 1 | 2 |\n"
)
_CLAIMS = [
    {"text": "Attention scales quadratically.", "confidence": "well-supported"},
    {"text": "Linear variants exist.", "confidence": "contested"},
]
_INFOBOX = {"Field": "NLP", "Introduced": "2017"}


def test_deterministic_byte_identical() -> None:
    a = compile_page_html(title=_TITLE, body_md=_BODY, claims=_CLAIMS, infobox=_INFOBOX)
    b = compile_page_html(title=_TITLE, body_md=_BODY, claims=_CLAIMS, infobox=_INFOBOX)
    assert a == b
    assert a.encode("utf-8") == b.encode("utf-8")


def test_no_script_and_no_external_refs() -> None:
    html = compile_page_html(
        title="<x>", body_md="<script>alert(1)</script> body", claims=[], infobox=None
    )
    lower = html.lower()
    assert "<script" not in lower
    # markdown-it renders with html disabled → the raw tag is escaped, not live.
    assert "&lt;script&gt;" in lower
    # No external resource references (CDNs, fonts, images).
    assert "http://" not in lower
    assert "https://" not in lower
    assert "//cdn" not in lower


def test_escapes_user_content() -> None:
    html = compile_page_html(
        title="Title & <b>bold</b>",
        body_md="plain",
        claims=[{"text": "<img src=x onerror=1>", "confidence": "<i>x</i>"}],
        infobox={"<k>": "<v>"},
    )
    assert "Title &amp; &lt;b&gt;bold&lt;/b&gt;" in html
    assert "&lt;img src=x onerror=1&gt;" in html
    assert "<img" not in html.lower()
    assert "&lt;k&gt;" in html
    assert "&lt;v&gt;" in html


def test_infobox_rendered_when_present_absent_when_not() -> None:
    with_box = compile_page_html(title="T", body_md="b", claims=[], infobox=_INFOBOX)
    assert 'class="infobox"' in with_box
    assert "Introduced" in with_box
    assert "2017" in with_box

    without = compile_page_html(title="T", body_md="b", claims=[], infobox=None)
    assert 'class="infobox"' not in without


def test_infobox_order_deterministic() -> None:
    # Same key/value pairs in the same insertion order → identical output.
    box1 = {"a": "1", "b": "2", "c": "3"}
    box2 = {"a": "1", "b": "2", "c": "3"}
    assert compile_page_html(title="T", body_md="x", infobox=box1) == compile_page_html(
        title="T", body_md="x", infobox=box2
    )


def test_claims_render_with_confidence() -> None:
    html = compile_page_html(title="T", body_md="b", claims=_CLAIMS, infobox=None)
    assert "Attention scales quadratically." in html
    assert "well-supported" in html
    assert "contested" in html
    assert 'class="claims"' in html


def test_self_contained_document_shape() -> None:
    html = compile_page_html(title="T", body_md="b", claims=[], infobox=None)
    assert html.startswith("<!doctype html>")
    assert "<style>" in html
    assert html.rstrip().endswith("</html>")

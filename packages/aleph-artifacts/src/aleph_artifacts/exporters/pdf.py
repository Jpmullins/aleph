"""Markdown → PDF exporter.

Uses WeasyPrint for HTML→PDF after a small markdown-it-py conversion.
Higher-fidelity backends (Prince) are pluggable by passing a different
`renderer` callable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_DEFAULT_CSS = """
@page { size: A4; margin: 22mm 18mm 22mm 18mm; }
body { font-family: Inter, sans-serif; font-size: 11pt; color: #0f172a; }
h1, h2, h3 { color: #1e293b; }
code, pre { font-family: "JetBrains Mono", monospace; font-size: 9pt; }
table { width: 100%; border-collapse: collapse; }
th, td { border: 1px solid #cbd5e1; padding: 4px 6px; }
"""


def _markdown_to_html(md: str) -> str:
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark").enable("table").render(md)


def markdown_to_pdf_bytes(
    *,
    report_md: str,
    title: str = "",
    css: str | None = None,
    renderer: Callable[[str, str], bytes] | None = None,
) -> bytes:
    html = (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title><style>{css or _DEFAULT_CSS}</style></head>"
        f"<body>{_markdown_to_html(report_md)}</body></html>"
    )
    if renderer is not None:
        return renderer(html, title)
    try:
        from weasyprint import HTML

        return HTML(string=html).write_pdf()  # type: ignore[no-any-return]
    except ImportError as exc:
        msg = "weasyprint is required for PDF export"
        raise RuntimeError(msg) from exc

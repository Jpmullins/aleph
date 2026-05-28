"""CSL-JSON → formatted bibliography.

Uses `citeproc-py` with bundled CSL XML styles. The Builder agent
passes a list of CSL-JSON items + a chosen style name; the formatter
returns a rendered bibliography (markdown by default, HTML if asked).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

_STYLES_DIR = Path(__file__).parent / "styles"

_BUNDLED = ("apa-7", "chicago-author-date", "ieee", "vancouver")


def list_bundled_styles() -> list[str]:
    return list(_BUNDLED)


def format_bibliography(
    *,
    items: list[dict[str, Any]],
    style: str = "apa-7",
    output: Literal["markdown", "html", "plain"] = "markdown",
) -> str:
    """Format the CSL-JSON `items` per `style`.

    If `citeproc-py` is available and a bundled style XML exists, use
    it. Otherwise fall back to a clean deterministic plain-author-year
    formatter so the Builder is never blocked by missing styles.
    """
    try:
        from citeproc import (
            Citation,
            CitationItem,
            CitationStylesBibliography,
            CitationStylesStyle,
            formatter,
        )
        from citeproc.source.json import CiteProcJSON
    except ImportError:
        return _fallback_bibliography(items, output=output)

    style_path = _STYLES_DIR / f"{style}.csl"
    if not style_path.exists():
        return _fallback_bibliography(items, output=output)

    bib_source = CiteProcJSON(items)
    bib_style = CitationStylesStyle(str(style_path), validate=False)
    fmt = (
        formatter.html
        if output == "html"
        else formatter.plain
    )
    bib = CitationStylesBibliography(bib_style, bib_source, fmt)
    # Register all items so citation order is stable.
    for item in items:
        if "id" in item:
            bib.register(Citation([CitationItem(item["id"])]))
    rendered_lines = [str(b) for b in bib.bibliography()]
    if output == "markdown":
        return "\n".join(f"- {line}" for line in rendered_lines)
    return "\n".join(rendered_lines)


def _fallback_bibliography(
    items: list[dict[str, Any]],
    *,
    output: Literal["markdown", "html", "plain"] = "markdown",
) -> str:
    lines: list[str] = []
    for item in items:
        authors = ", ".join(
            f"{a.get('family', '')}, {a.get('given', '')[:1]}".rstrip(", ")
            for a in item.get("author", []) or []
        )
        year = (
            item.get("issued", {}).get("date-parts", [[None]])[0][0]
            if isinstance(item.get("issued"), dict)
            else None
        )
        title = item.get("title", "")
        venue = item.get("container-title") or item.get("publisher") or ""
        line = f"{authors} ({year or 'n.d.'}). {title}."
        if venue:
            line += f" {venue}."
        lines.append(line)
    if output == "markdown":
        return "\n".join(f"- {line}" for line in lines)
    if output == "html":
        return "<ol>\n" + "\n".join(f"  <li>{line}</li>" for line in lines) + "\n</ol>"
    return "\n".join(lines)

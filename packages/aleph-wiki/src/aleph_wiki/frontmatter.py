"""YAML frontmatter — the bridge between a markdown vault and the database.

Aleph stores the governance fields as columns (see `models.WikiPage`) because
they have to be filtered, grouped and linted on. Obsidian stores them as YAML
at the top of the file. Both are the same data, and this module is the only
place that knows how to move between them.

Keeping that knowledge in one module matters more than it looks: a wiki whose
frontmatter is written by one code path and read by another drifts into pages
that render correctly and query wrongly — the body says `confidence: high` and
the column says nothing, and which one is true depends on who is asking. Every
read goes through `parse`; every write goes through `render`.

The field names, ordering and value vocabulary follow the hermes-agent
`llm-wiki` schema exactly, so a page written here opens in Obsidian with its
Dataview queries intact, and a vault authored in Obsidian imports without
translation.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import yaml

__all__ = ["Frontmatter", "extract_wikilinks", "parse", "render", "strip"]

#: Frontmatter is the block between the first two `---` fences, and only when
#: the file opens with one. A `---` further down is a horizontal rule and must
#: not be treated as a fence — matching it would silently eat a page's body.
_FENCE = re.compile(r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

#: `[[slug]]` and `[[slug|display text]]`. Anchors (`[[slug#section]]`) resolve
#: to the page, so the fragment is dropped rather than making a distinct link.
_WIKILINK = re.compile(r"\[\[([^\]\[|#]+)(?:#[^\]\[|]*)?(?:\|[^\]\[]*)?\]\]")

#: Field order in the rendered block. Explicit rather than alphabetical because
#: a human scanning a vault reads identity, then filing, then quality — and a
#: stable order keeps diffs to what actually changed.
_ORDER = (
    "title",
    "aliases",
    "created",
    "updated",
    "type",
    "category",
    "tags",
    "related",
    "sources",
    "confidence",
    "contested",
    "contradictions",
)


@dataclass(slots=True)
class Frontmatter:
    """The typed frontmatter block.

    Optional fields are `None`/empty rather than defaulted to something
    plausible. `confidence=None` means nobody judged the page, which is a
    different claim from `confidence="low"` and is what the lint reports.
    """

    title: str = ""
    aliases: list[str] = field(default_factory=list)
    created: date | None = None
    updated: date | None = None
    type: str | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: str | None = None
    contested: bool = False
    contradictions: list[str] = field(default_factory=list)

    def to_yaml_dict(self) -> dict[str, Any]:
        """Serialisable form, dropping empties so the block stays readable.

        A page carrying `contested: false`, `contradictions: []`,
        `confidence: null` on every one of 251 pages is noise that trains a
        reader to skip the block entirely. Absent means absent.
        """
        raw = asdict(self)
        out: dict[str, Any] = {}
        for key in _ORDER:
            value = raw.get(key)
            if value in (None, "", [], False):
                # `contested: true` is meaningful; `false` is the default and
                # is left out. Same for empty lists and unset dates.
                continue
            out[key] = value.isoformat() if isinstance(value, date) else value
        return out


def _as_str_list(value: Any) -> list[str]:
    """Coerce a YAML scalar-or-sequence into a list of strings.

    Hand-written frontmatter routinely says `tags: architecture` where the
    schema wants a list. Accepting both on read is not laxity — rejecting it
    would mean a human-authored vault fails to import over a comma.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()]


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def parse(body_md: str) -> tuple[Frontmatter, str]:
    """Split a markdown body into its frontmatter and the rest.

    A body with no frontmatter, or with YAML that does not parse, returns an
    empty `Frontmatter` and the body untouched. That is deliberate: this runs
    over a corpus that predates the schema, and a parse error must degrade to
    "this page has no frontmatter yet" rather than making the page unreadable.
    The lint is what reports the absence.
    """
    match = _FENCE.match(body_md)
    if match is None:
        return Frontmatter(), body_md

    rest = body_md[match.end() :]
    try:
        loaded = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError:
        return Frontmatter(), body_md
    if not isinstance(loaded, dict):
        return Frontmatter(), body_md

    raw: dict[str, Any] = loaded
    return (
        Frontmatter(
            title=str(raw.get("title") or "").strip(),
            aliases=_as_str_list(raw.get("aliases")),
            created=_as_date(raw.get("created")),
            updated=_as_date(raw.get("updated")),
            type=(str(raw["type"]).strip() or None) if raw.get("type") else None,
            category=(str(raw["category"]).strip() or None) if raw.get("category") else None,
            tags=_as_str_list(raw.get("tags")),
            related=_as_str_list(raw.get("related")),
            sources=_as_str_list(raw.get("sources")),
            confidence=(
                (str(raw["confidence"]).strip() or None) if raw.get("confidence") else None
            ),
            contested=bool(raw.get("contested") or False),
            contradictions=_as_str_list(raw.get("contradictions")),
        ),
        rest,
    )


def strip(body_md: str) -> str:
    """Return the body with any frontmatter removed."""
    return parse(body_md)[1]


def render(fm: Frontmatter, body: str) -> str:
    """Re-attach a frontmatter block to a body.

    `body` must already be frontmatter-free — pass the second element of
    `parse`, never the original string, or the page ends up with two blocks and
    Obsidian reads the first while this module's next `parse` also reads the
    first, so the second silently becomes prose.
    """
    data = fm.to_yaml_dict()
    if not data:
        return body.lstrip("\n")
    block = yaml.safe_dump(
        data,
        sort_keys=False,  # `_ORDER` is the order; alphabetical would scramble it
        allow_unicode=True,
        default_flow_style=None,  # short lists stay inline: `tags: [a, b]`
        width=100,
    ).rstrip()
    return f"---\n{block}\n---\n\n{body.lstrip(chr(10))}"


def extract_wikilinks(body_md: str) -> list[str]:
    """Every `[[target]]` in the body, deduplicated, in order of first use.

    Frontmatter is excluded: `related:` names slugs too, but those are a curated
    "see also", not links a reader can follow from the prose. Counting them
    toward the minimum-outbound-links rule would let a page satisfy the rule
    while its body links to nothing.
    """
    seen: dict[str, None] = {}
    for raw in _WIKILINK.findall(strip(body_md)):
        target = raw.strip()
        if target:
            seen.setdefault(target, None)
    return list(seen)

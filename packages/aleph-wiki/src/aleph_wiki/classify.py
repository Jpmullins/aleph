"""Derive a schema from a corpus, and file a corpus into a schema.

Aleph ships a default schema describing AI/ML research, because the first
plugin suite is research. That default is a guess, and on any real project it
is usually the wrong one — the corpus this was first run against is about
database storage systems (LSM trees, write-ahead logging, crash recovery), for
which "post-training" and "multimodal" are not categories, they are noise.

A wrong taxonomy is worse than none. It gives every page a plausible-looking
home, so nothing reports a problem, and the categories quietly stop meaning
anything. So rather than making the owner hand-write a taxonomy before the wiki
is usable, the schema is *proposed from the corpus that exists* and then edited.

Two operations, deliberately separate:

- `propose_schema` reads the page titles and proposes a domain, categories and
  a tag taxonomy. One call. The result is a proposal — nothing is written until
  someone accepts it, because the taxonomy governs every later write.
- `classify_pages` files pages into whatever schema is in force. Batched, and
  it never invents a category: a page the model cannot place is left
  uncategorised, which the lint already reports. Guessing would produce exactly
  the plausible-looking wrong home this module exists to avoid.

Both go through `LiteLLMClient` with a `Capability`, so they are costed and
routed like every other model call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage, ChatResponse, LiteLLMClient
from aleph_wiki.models import WikiPage
from aleph_wiki.schema import Category, WikiSchema

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aleph_security.principal import Principal

__all__ = ["ClassificationResult", "classify_pages", "propose_schema"]

#: Titles per classification call. Large enough that 679 pages is ~17 calls
#: rather than 679, small enough that the model still attends to each title —
#: past roughly this size, accuracy on the last items in the batch degrades.
BATCH = 40

#: Titles sampled when proposing a schema. The proposal is about the shape of
#: the corpus, and a sample states that as well as the whole does while fitting
#: in one context window.
SCHEMA_SAMPLE = 300


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    filed: int
    skipped: int
    unknown_category: int

    def summary(self) -> str:
        return (
            f"{self.filed} pages filed, {self.skipped} left uncategorised "
            f"(the model declined to place them), {self.unknown_category} "
            "proposed a category not in the schema and were rejected"
        )


def _content(response: ChatResponse) -> str:
    """The assistant's text, or empty when the provider returned no choice."""
    return (response.choices[0].message.content if response.choices else "") or ""


def _json_object(text: str) -> dict[str, Any]:
    """Parse a model response that should be one JSON object.

    Tolerates the ```json fences models add even when told not to. A parse
    failure returns `{}` rather than raising: the caller treats an unparseable
    response as "the model declined", which is the same safe outcome as a
    refusal, instead of failing a 17-batch run on one malformed reply.
    """
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        loaded = json.loads(body)
    except (ValueError, TypeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


async def _titles(session: AsyncSession, project_id: UUID, *, limit: int) -> list[str]:
    """A sample of real page titles, written pages first.

    Written pages describe the corpus; stubs describe what it mentions. Ordering
    written-first means the sample states what the wiki is actually about even
    when stubs outnumber pages ten to one, which they do.
    """
    rows = (
        await session.execute(
            select(WikiPage.title)
            .where(WikiPage.project_id == project_id)
            .order_by(WikiPage.is_stub, WikiPage.title)
            .limit(limit)
        )
    ).scalars()
    return list(rows)


_SCHEMA_PROMPT = """\
You are proposing the governance schema for a knowledge wiki, from the titles \
of the pages it already contains.

Return ONE JSON object, no prose, no code fences:

{
  "domain": "One or two sentences naming what this wiki covers, specifically. \
Name the actual field, not a generic phrase.",
  "categories": [
    {"id": "kebab-case", "title": "Title Case", "blurb": "what belongs here"}
  ],
  "tags": ["kebab-case", ...]
}

Rules:
- 6 to 12 categories. They must partition THIS corpus — every category must \
have pages in the list below, and together they should cover most of it. Do \
not include categories for topics that are absent.
- 20 to 40 tags, forming a controlled vocabulary: a tag is a facet that cuts \
ACROSS categories (a method, a property, a kind of artifact), not a synonym \
for a category.
- Prefer one word per tag. Never include two tags that mean the same thing — \
the point of the taxonomy is that there is exactly one way to say each thing.
- ids and tags: lowercase, hyphens, no spaces.

Page titles:
"""


async def propose_schema(
    session: AsyncSession,
    *,
    project_id: UUID,
    client: LiteLLMClient,
    principal: Principal,
    profile_bindings: dict[str, Any],
    current: WikiSchema,
) -> WikiSchema | None:
    """Propose a schema fitting the corpus that exists.

    Returns None when there is nothing to go on, or when the model's proposal
    does not parse — the caller keeps the current schema rather than adopting a
    half-formed one. Nothing is persisted here; `SchemaService.set` is the only
    write path, and it takes a ledger event.
    """
    titles = await _titles(session, project_id, limit=SCHEMA_SAMPLE)
    if len(titles) < 5:
        return None

    response = await client.chat(
        principal=principal,
        project_id=project_id,
        agent_run_id=None,
        capability=Capability.CLASSIFICATION,
        profile_bindings=profile_bindings,
        messages=[
            ChatMessage(role="user", content=_SCHEMA_PROMPT + "\n".join(f"- {t}" for t in titles))
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        purpose="wiki.schema.propose",
    )
    raw = _json_object(_content(response))
    categories = raw.get("categories") or []
    tags = raw.get("tags") or []
    if not isinstance(categories, list) or not isinstance(tags, list) or not categories:
        return None

    parsed: list[Category] = []
    for entry in categories:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        cid = str(entry["id"]).strip().lower().replace(" ", "-")
        parsed.append(
            Category(
                id=cid,
                title=str(entry.get("title") or cid.replace("-", " ").title()),
                blurb=str(entry.get("blurb") or ""),
            )
        )
    if not parsed:
        return None

    # Thresholds are not the model's business — they encode how this corpus was
    # produced (machine-extracted links are cheap), which the titles say nothing
    # about. Carry the ones in force.
    return WikiSchema(
        domain=str(raw.get("domain") or current.domain),
        categories=tuple(parsed),
        tags=tuple(dict.fromkeys(str(t).strip().lower().replace(" ", "-") for t in tags if t)),
        page_types=current.page_types,
        min_outbound_links=current.min_outbound_links,
        page_split_lines=current.page_split_lines,
        stub_promotion_mentions=current.stub_promotion_mentions,
    )


_CLASSIFY_PROMPT = """\
File each wiki page under exactly one category, give it a type, and tag it.

Categories (use the id, nothing else):
{categories}

Types: {types}

Tags (use ONLY these, 2-4 per page):
{tags}

Return ONE JSON object mapping each title verbatim to its filing, no prose:

{{"Some Page Title": {{"category": "id", "type": "concept", "tags": ["a","b"]}}}}

If a page does not belong in any category, map it to null. Do NOT force a fit:
a page filed in a category it does not belong to is worse than one left
unfiled, because nothing will ever report it as wrong.

Titles:
"""


async def classify_pages(
    session: AsyncSession,
    *,
    project_id: UUID,
    schema: WikiSchema,
    client: LiteLLMClient,
    principal: Principal,
    profile_bindings: dict[str, Any],
    include_stubs: bool = True,
    limit: int | None = None,
) -> ClassificationResult:
    """File unfiled pages into the schema in force.

    Only pages with no category are touched, so this is resumable and safe to
    re-run: a page somebody filed by hand is never silently refiled by a model.

    Stubs are included by default. A stub has no body, but its title is exactly
    what a category is — and an uncategorised stub is invisible in every hub,
    which is most of the corpus.
    """
    stmt = (
        select(WikiPage)
        .where(WikiPage.project_id == project_id, WikiPage.category.is_(None))
        .order_by(WikiPage.is_stub, WikiPage.title)
    )
    if not include_stubs:
        stmt = stmt.where(WikiPage.is_stub.is_(False))
    if limit is not None:
        stmt = stmt.limit(limit)
    pages = list((await session.execute(stmt)).scalars().all())
    if not pages:
        return ClassificationResult(0, 0, 0)

    header = _CLASSIFY_PROMPT.format(
        categories="\n".join(
            f"- {c.id}: {c.title}" + (f" — {c.blurb}" if c.blurb else "") for c in schema.categories
        ),
        types=", ".join(t for t in schema.page_types if t != "raw-source"),
        tags=", ".join(schema.tags),
    )

    filed = skipped = unknown = 0
    by_title = {p.title: p for p in pages}

    for start in range(0, len(pages), BATCH):
        batch = pages[start : start + BATCH]
        response = await client.chat(
            principal=principal,
            project_id=project_id,
            agent_run_id=None,
            capability=Capability.CLASSIFICATION,
            profile_bindings=profile_bindings,
            messages=[
                ChatMessage(role="user", content=header + "\n".join(f"- {p.title}" for p in batch))
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            purpose="wiki.page.classify",
        )
        answers = _json_object(_content(response))

        for title, filing in answers.items():
            page = by_title.get(str(title))
            if page is None or page.category is not None:
                continue
            if not isinstance(filing, dict):
                skipped += 1
                continue
            category = str(filing.get("category") or "").strip()
            # The schema is the authority, not the model. A category outside it
            # is rejected rather than adopted — otherwise the taxonomy grows by
            # whatever a model happened to emit, which is the sprawl the
            # controlled vocabulary exists to prevent.
            if category not in schema.category_ids:
                unknown += 1
                continue
            page.category = category
            page_type = str(filing.get("type") or "").strip()
            if page_type in schema.page_types:
                page.page_type = page_type
            elif not page.is_stub:
                page.page_type = "concept"
            proposed = filing.get("tags")
            if isinstance(proposed, list):
                page.tags = [t for t in (str(x).strip() for x in proposed) if t in schema.tag_set][
                    :5
                ]
            filed += 1

        skipped += sum(1 for p in batch if p.category is None and p.title not in answers)

    await session.flush()
    return ClassificationResult(filed=filed, skipped=skipped, unknown_category=unknown)

"""The central-hypothesis probe: can retrieval find a page by words in its body?

This is the assertion the project never had. The 2026-08 review concluded the
wiki bet "was never actually placed" because `wiki_index.index_tsv` is built
from title + summary + aliases and never from `body_md`, and the query gate is
`plainto_tsquery`, which ANDs every term. Between them, a question phrased in
the words a page actually uses cannot retrieve that page.

Nothing here calls an LLM. Candidate generation is pure Postgres, which is
exactly where the defect lives, so these run in CI against the integration
service with no gateway key.

`test_body_phrase_retrieves_its_page` is EXPECTED TO FAIL against the current
implementation. It is not marked xfail: it is the acceptance test for the
retrieval work in docs/belief-engine.md, and it should stay red until that
lands and then stay green forever. A red test that names the defect is worth
more than a green one that does not look.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# A body that shares NO content word with the title or the summary. The probe
# phrase must exist only in body_md, or the test proves nothing.
TITLE = "Coastal Sediment Study"
SUMMARY = "An overview of the coastal sediment study and its scope."
BODY = """# Coastal Sediment Study

## Methodology

Cores were retrieved using a vibracorer deployed from a shallow-draft vessel.
Grain-size distribution was determined by laser diffraction granulometry, and
the resulting spectra were deconvolved with an endmember mixing model.

## Findings

The dominant lithofacies is a moderately sorted quartzarenite exhibiting
hummocky cross-stratification, consistent with storm-dominated deposition
above fair-weather wave base.
"""

# Words present in BODY and absent from TITLE + SUMMARY.
BODY_ONLY_PHRASE = "vibracorer granulometry quartzarenite"
BODY_ONLY_SINGLE = "quartzarenite"


async def _make_page(maker, project_id):
    """Commit one wiki page and refresh its index row, the way ingest does."""
    from aleph_wiki.index_service import IndexService
    from aleph_wiki.models import WikiPage, WikiRevision

    page_id, revision_id = uuid7(), uuid7()
    async with maker() as session:
        session.add(
            WikiPage(
                id=page_id,
                project_id=project_id,
                title=TITLE,
                slug="coastal-sediment-study",
                page_kind="topic",
                current_revision_id=revision_id,
                is_stub=False,
                status="approved",
                created_by=uuid4(),
                access_scope="project",
            )
        )
        session.add(
            WikiRevision(
                id=revision_id,
                page_id=page_id,
                project_id=project_id,
                revision_no=1,
                body_md=BODY,
                summary=SUMMARY,
                author_kind="agent",
                author_id=uuid4(),
                parent_revision_id=None,
                body_sha256="0" * 64,
                commit_message="probe fixture",
                ledger_event_id=uuid7(),
            )
        )
        await session.flush()
        await IndexService(session).refresh_page(
            project_id=project_id, page_id=page_id, summary=SUMMARY
        )
        await session.commit()
    return page_id


async def test_title_phrase_retrieves_its_page(asgi_app):
    """Control. Title words DO match — this proves the fixture and index work."""
    from aleph_wiki.index_service import IndexService

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    page_id = await _make_page(maker, project_id)

    async with maker() as session:
        hits = await IndexService(session).select_pages(
            project_id=project_id, query="coastal sediment", top_k=8
        )

    assert page_id in {h.page_id for h in hits}, (
        "the control failed: even title words did not retrieve the page, so this "
        "test file is not measuring what it claims to measure"
    )


async def test_body_phrase_retrieves_its_page(asgi_app):
    """THE PROBE. Words that appear only in body_md must retrieve the page.

    Expected to fail until retrieval indexes page bodies. See
    docs/belief-engine.md and docs/decisions.md D1.
    """
    from aleph_wiki.index_service import IndexService

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    page_id = await _make_page(maker, project_id)

    async with maker() as session:
        hits = await IndexService(session).select_pages(
            project_id=project_id, query=BODY_ONLY_SINGLE, top_k=8
        )

    assert page_id in {h.page_id for h in hits}, (
        f"a page could not be retrieved by {BODY_ONLY_SINGLE!r}, a word that appears "
        f"in its body and nowhere else. wiki_index.index_tsv is built from "
        f"title + summary + aliases only, so page bodies are unsearchable. "
        f"got {len(hits)} hit(s)."
    )


async def test_natural_language_question_retrieves_its_page(asgi_app):
    """The AND-semantics probe.

    `plainto_tsquery` conjoins every term, so a question phrased naturally must
    have ALL of its content words present in the ~2KB indexed text. Real
    questions do not. Expected to fail until the gate becomes
    `websearch_to_tsquery` (OR + phrases) over an index that includes bodies.
    """
    from aleph_wiki.index_service import IndexService

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    page_id = await _make_page(maker, project_id)

    async with maker() as session:
        hits = await IndexService(session).select_pages(
            project_id=project_id,
            query="what depositional environment does the sediment record indicate",
            top_k=8,
        )

    assert page_id in {h.page_id for h in hits}, (
        "a natural-language question about the page's own subject retrieved "
        "nothing. plainto_tsquery ANDs every term, so every content word must "
        "appear in title+summary+aliases for the page to be a candidate at all."
    )


async def test_expansion_ignores_links_from_superseded_revisions(asgi_app):
    """A link removed in a later revision must stop pulling its target into context.

    WikiLink rows are per-revision and never deleted — wiki_service's
    `delete(WikiLink).where(src_revision_id == revision_id)` targets the
    brand-new revision id and is therefore always a no-op. So a page rewritten
    N times has N sets of outgoing links in the table, and the expansion query
    must join on the page's CURRENT revision or it walks the union of all of
    them forever.
    """
    from sqlalchemy import select

    from aleph_wiki.models import WikiLink, WikiPage, WikiRevision

    maker = asgi_app.state.session_maker
    project_id = uuid7()
    src_id = await _make_page(maker, project_id)

    dropped_id, kept_id = uuid7(), uuid7()
    old_rev, new_rev = uuid7(), uuid7()

    async with maker() as session:
        for pid, title, slug in (
            (dropped_id, "Dropped Target", "dropped-target"),
            (kept_id, "Kept Target", "kept-target"),
        ):
            session.add(
                WikiPage(
                    id=pid,
                    project_id=project_id,
                    title=title,
                    slug=slug,
                    page_kind="topic",
                    current_revision_id=None,
                    is_stub=False,
                    status="approved",
                    created_by=uuid4(),
                    access_scope="project",
                )
            )
        # Revision 2 supersedes revision 1 on the source page.
        session.add(
            WikiRevision(
                id=new_rev,
                page_id=src_id,
                project_id=project_id,
                revision_no=2,
                body_md=BODY,
                summary=SUMMARY,
                author_kind="agent",
                author_id=uuid4(),
                parent_revision_id=old_rev,
                body_sha256="1" * 64,
                commit_message="dropped a link",
                ledger_event_id=uuid7(),
            )
        )
        # The old revision linked to Dropped; the current one links to Kept.
        session.add(
            WikiLink(
                id=uuid7(),
                project_id=project_id,
                src_page_id=src_id,
                src_revision_id=old_rev,
                dst_page_id=dropped_id,
                dst_title="Dropped Target",
                occurrences=9,  # high, so a broken query would rank it FIRST
            )
        )
        session.add(
            WikiLink(
                id=uuid7(),
                project_id=project_id,
                src_page_id=src_id,
                src_revision_id=new_rev,
                dst_page_id=kept_id,
                dst_title="Kept Target",
                occurrences=1,
            )
        )
        page = (await session.execute(select(WikiPage).where(WikiPage.id == src_id))).scalar_one()
        page.current_revision_id = new_rev
        await session.commit()

    from aleph_assistant.retrieval.router import WikiFirstRetrievalRouter

    router = WikiFirstRetrievalRouter(session_maker=maker, litellm=None)  # type: ignore[arg-type]
    expanded = await router._expand(
        project_id=project_id,
        primary_ids=[src_id],
        already_selected={src_id},
        limit=8,
    )
    got = {p.page_id for p in expanded}

    assert kept_id in got, "the current revision's link was not followed"
    assert dropped_id not in got, (
        "expansion followed a link that only exists on a superseded revision — "
        "removed links keep pulling their targets into answer context forever"
    )

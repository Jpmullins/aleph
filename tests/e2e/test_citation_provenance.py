"""The provenance chain must carry a real join key, written by production code.

`Citation.source_page_id` was NULL at every production write site. Both
originating `CitationDraft` producers hardcoded `None` — one with the comment
"Set in commit step (self-citation)", naming a step that never did it — and the
other two paths copy. Every mechanism that walks
``Source → SourcePage → Citation → WikiClaim`` therefore INNER-JOINed on NULL
and returned empty on real data while reporting success: the retraction blast
radius, freshness citation-health and source-freshness, the refresh fact-diff,
and the mechanical reviewer's stale-source and DOI passes.

It survived a whole work package because the only tests that exercised the chain
hand-built the bridge row *and* hand-passed `source_page_id=bridge.id`, so they
never touched the production writer.

**These tests therefore construct nothing on the chain.** They drive the real
`_node_commit_revision` — the node that makes and breaks the promise — and then
assert on what is actually in the database. Per END-STATE.md's fixture rule: the
value under test must be produced by the code path production uses.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.time import utcnow

pytestmark = pytest.mark.integration


async def _seed_project_and_source(session, principal):
    """A project + Source, via the same repos production uses."""
    from aleph_db.models.model_profile import ModelProfile
    from aleph_db.repos import project as project_repo
    from aleph_rks.models import Source

    profile = ModelProfile(
        id=uuid7(),
        project_id=None,
        name=f"t-{uuid4().hex[:8]}",
        is_template=False,
        bindings_jsonb={},
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(profile)
    await session.flush()

    project = project_repo.new_project(
        title="Citation provenance",
        description="",
        model_profile_id=profile.id,
        created_by=principal.user_id,
    )
    session.add(project)
    await session.flush()

    source = Source(
        id=uuid7(),
        project_id=project.id,
        short_id=f"S{uuid4().hex[:4].upper()}",
        title="A Cited Source",
        url="https://example.invalid/paper",
        connector_kind="upload",
        status="normalized",
        source_metadata_jsonb={},
        created_by=principal.user_id,
        access_scope="project",
    )
    session.add(source)
    await session.flush()
    return project.id, source


async def _run_commit_node(asgi_app, principal, project_id, source):
    """Drive the REAL commit node with a draft shaped exactly as the compose
    node builds it — `source_page_id=None`, the state that was never repaired."""
    from aleph_wiki.agent import workflow as wf
    from aleph_wiki.agent.workflow import WikiPageDraft
    from aleph_wiki.wiki_service import CitationDraft, ClaimDraft

    draft = WikiPageDraft(
        page_kind="source",
        title=f"Source: {source.title}",
        slug=f"source-{source.short_id.lower()}",
        page_id=None,
        body_md=f"# {source.title}\n\n## Key claims\n\nThe effect holds [c1].\n",
        summary="Source page.",
        claims=[
            ClaimDraft(
                text="The effect holds.",
                confidence="cited",
                section_anchor="key-claims",
                citations=[
                    # EXACTLY what `_node_source_page_compose` produces.
                    CitationDraft(chunk_ids=[], source_page_id=None, citation_marker="[c1]")
                ],
            )
        ],
        wikilinks=[],
        commit_message=f"Ingest source {source.short_id}",
        addressed_feedback_ids=[],
    )

    ctx = wf.WorkflowContext(
        session_maker=asgi_app.state.session_maker,
        litellm=asgi_app.state.litellm,
        principal=principal,
    )
    # The workflow context moved into a ContextVar: arq runs jobs concurrently
    # in one event loop, and the module global let one job clear another's
    # context mid-run ("WikiIngestWorkflow context not initialized").
    token = wf._active_ctx_var.set(ctx)
    try:
        return await wf._node_commit_revision(
            {
                "project_id": project_id,
                "source_id": source.id,
                "source_title": source.title,
                "source_short_id": source.short_id,
                "source_page_draft": draft,
                "topic_page_drafts": [],
            }
        )
    finally:
        wf._active_ctx_var.reset(token)


async def test_commit_writes_a_resolvable_citation(asgi_app):
    """The headline: after the production commit node runs, the chain resolves.

    Asserts the *whole* walk, not just non-NULL — a non-NULL id pointing at
    nothing would satisfy a weaker check while leaving every consumer empty.
    """
    from aleph_security.principal import Principal
    from aleph_wiki.models import Citation, SourcePage, WikiClaim

    maker = asgi_app.state.session_maker
    principal = Principal(
        user_id=(await _dev_user_id(maker)),
        subject="dev@aleph.local",
        email="dev@aleph.local",
        actor_kind="user",
    )

    async with maker() as session:
        project_id, source = await _seed_project_and_source(session, principal)
        await session.commit()

    await _run_commit_node(asgi_app, principal, project_id, source)

    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(Citation, SourcePage, WikiClaim)
                    .join(WikiClaim, WikiClaim.id == Citation.claim_id)
                    .join(SourcePage, SourcePage.id == Citation.source_page_id)
                    .where(Citation.project_id == project_id)
                )
            ).all()
        )

    assert rows, (
        "no citation resolved through Source → SourcePage → Citation → WikiClaim "
        "after the production commit node ran. This is the INNER JOIN every "
        "trust mechanism performs; empty here means all of them are inert."
    )
    _citation, source_page, _claim = rows[0]
    assert source_page.source_id == source.id, (
        "the citation resolved to the wrong source — the join key points at an "
        "unrelated SourcePage."
    )


async def test_citation_join_key_is_the_source_page_pk(asgi_app):
    """Pin the id-space the column is written in.

    Five readers resolve it as a `source_pages` PK and one (the citation
    popover) resolved it as a `wiki_pages` id. Both are unconstrained nullable
    UUIDs, so picking the wrong one fails silently rather than loudly. This
    fixes the meaning so a future writer cannot flip it.
    """
    from aleph_security.principal import Principal
    from aleph_wiki.models import Citation, SourcePage

    maker = asgi_app.state.session_maker
    principal = Principal(
        user_id=(await _dev_user_id(maker)),
        subject="dev@aleph.local",
        email="dev@aleph.local",
        actor_kind="user",
    )
    async with maker() as session:
        project_id, source = await _seed_project_and_source(session, principal)
        await session.commit()

    await _run_commit_node(asgi_app, principal, project_id, source)

    async with maker() as session:
        cite = (
            (await session.execute(select(Citation).where(Citation.project_id == project_id)))
            .scalars()
            .first()
        )
        bridge = (
            (await session.execute(select(SourcePage).where(SourcePage.source_id == source.id)))
            .scalars()
            .one()
        )

    assert cite is not None and cite.source_page_id == bridge.id, (
        f"citation.source_page_id={cite and cite.source_page_id} is not the "
        f"SourcePage PK {bridge.id}. If it holds the wiki page id "
        f"({bridge.page_id}) instead, retraction/freshness/refresh/mechanical "
        "all silently return empty."
    )


async def test_freshness_now_sees_the_citation(asgi_app):
    """The consumer that mattered: freshness citation-health is no longer inert.

    `CuratorService._recompute_freshness` skipped every citation on
    `if cite.source_page_id is None: continue`, so `ClaimCitation.source_ids`
    was always empty and `_citation_health` scored 0 for any page WITH claims.
    """
    from aleph_security.principal import Principal
    from aleph_wiki.models import Citation, SourcePage

    maker = asgi_app.state.session_maker
    principal = Principal(
        user_id=(await _dev_user_id(maker)),
        subject="dev@aleph.local",
        email="dev@aleph.local",
        actor_kind="user",
    )
    async with maker() as session:
        project_id, source = await _seed_project_and_source(session, principal)
        await session.commit()

    await _run_commit_node(asgi_app, principal, project_id, source)

    # Reproduce the curator's resolution loop verbatim.
    async with maker() as session:
        cites = list(
            (await session.execute(select(Citation).where(Citation.project_id == project_id)))
            .scalars()
            .all()
        )
        source_ids: list = []
        for cite in cites:
            if cite.source_page_id is None:
                continue
            sp = await session.get(SourcePage, cite.source_page_id)
            if sp is None:
                continue
            source_ids.append(sp.source_id)

    assert source_ids, (
        "the curator's resolution loop still yields no source ids — "
        "citation-health remains hardwired to 0 for every page with claims."
    )
    assert source.id in source_ids


async def _dev_user_id(maker):
    """Reuse the JIT-provisioned dev user rather than inventing an actor id."""
    from aleph_db.models.identity import User

    async with maker() as session:
        user = (
            await session.execute(select(User).where(User.subject == "dev@aleph.local"))
        ).scalar_one_or_none()
        if user is not None:
            return user.id
        user = User(
            id=uuid7(),
            subject="dev@aleph.local",
            email="dev@aleph.local",
            display_name="Dev",
            created_by=uuid7(),
            access_scope="global",
            created_at=utcnow(),
        )
        session.add(user)
        await session.commit()
        return user.id

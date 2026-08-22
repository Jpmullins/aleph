"""`POST /v1/projects/{id}/export/vault` carries the belief layer (WS-H8).

The format half is covered without a database in
`packages/aleph-wiki/tests/test_export_evidence.py` and
`tests/unit/test_vault_evidence_bundle.py`. What needs Postgres is the half
that decides *which evidence gets exported*: the live-claims filter, the stub
filter, the left join to a source that may be gone, and — the one nothing else
can check — that the query's ordering is total enough for two exports of an
unchanged corpus to be byte-identical.

Every one of those is the shape a mocked session reports as working while the
real query returns the wrong set. A superseded-claim filter that silently
matched everything would still produce a zip, and the export would ship every
draft of every belief as though all of them were currently held.

The app is built without its lifespan; only `session_maker`, `settings` and the
principal seam are supplied, exactly as `test_vault_export_route.py` does.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, FastAPI, Path
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aleph_api.deps import principal_dep
from aleph_api.middleware.project_scope import project_scope_dep
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.models.project import Project
from aleph_rks.models import Source
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole
from aleph_wiki.export_evidence import EVIDENCE_FILENAME
from aleph_wiki.export_service import load_page_evidence
from aleph_wiki.models import Citation, WikiClaim, WikiPage, WikiRevision

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000cc")

QUOTE = "attention weights are computed over the whole sequence at once"
LIVE_CLAIM = "Self-attention replaces recurrence."
STUB_CLAIM = "A claim on a page nobody wrote."
SUPERSEDED_CLAIM = "An earlier wording nobody holds any more."


def _build_app(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    principal: Principal,
) -> FastAPI:
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    app.state.settings = SimpleNamespace(
        aleph_auth_mode="local",
        aleph_agent_token_secret="integration-secret-0123456789abcdef0123456789ab",
    )
    app.state.session_maker = maker

    async def _fake_local_dev(_request: Any) -> Principal:
        return principal

    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)

    async def _scope(
        project_id: Annotated[uuid.UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> uuid.UUID:
        p.cache_role(project_id, ProjectRole.OWNER.value)
        return project_id

    app.dependency_overrides[project_scope_dep] = _scope
    return app


async def _add_claim(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    page_id: uuid.UUID,
    body: str,
    superseded_by: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
) -> uuid.UUID:
    claim = WikiClaim(
        id=uuid7(),
        project_id=project_id,
        page_id=page_id,
        text=body,
        claim_key=None,
        superseded_by=superseded_by,
        origin="agent",
        evidence_tier="cited",
        confidence="weakly_supported",
        status="active",
        created_by=ACTOR,
    )
    session.add(claim)
    await session.flush()
    if source_id is not None:
        session.add(
            Citation(
                id=uuid7(),
                project_id=project_id,
                claim_id=claim.id,
                source_id=source_id,
                chunk_id=uuid7(),
                quote=QUOTE,
                verbatim=True,
                stance="supports",
                weight=1.0,
                locator_hash=None,
                char_start=120,
                char_end=120 + len(QUOTE),
                chunk_ids=[],
                citation_marker="[c1]",
            )
        )
        # A second citation with a source and no quote: half the live corpus is
        # in this state, and it is the row that separates `citations` from
        # `anchored_citations`.
        session.add(
            Citation(
                id=uuid7(),
                project_id=project_id,
                claim_id=claim.id,
                source_id=source_id,
                chunk_id=None,
                quote=None,
                verbatim=False,
                stance="supports",
                weight=1.0,
                locator_hash=None,
                char_start=None,
                char_end=None,
                chunk_ids=[],
                citation_marker="[c2]",
            )
        )
        await session.flush()
    return claim.id


async def _seed(session: AsyncSession, project_id: uuid.UUID) -> None:
    """One written page with evidence, one stub that also has a claim.

    The stub's claim is the interesting one: it is attached to a page the
    bundle does not contain, so exporting it would put a slug in
    `evidence.json` with no file next to it.
    """
    session.add(
        Project(
            id=project_id,
            title="Evidence Export Test",
            description="",
            status="active",
            model_profile_id=uuid7(),
            created_by=ACTOR,
        )
    )
    source_id = uuid7()
    session.add(
        Source(
            id=source_id,
            project_id=project_id,
            connector_kind="web",
            external_id=None,
            title="Attention Is All You Need",
            url="https://arxiv.org/abs/1706.03762",
            short_id=f"S{str(source_id)[-6:]}",
            status="ingested",
            created_by=ACTOR,
        )
    )
    pages: dict[str, uuid.UUID] = {}
    for title, slug, is_stub, body in (
        ("Attention", "attention", False, "Prose about attention.\n"),
        ("Recurrent Networks", "recurrent-networks", False, "Prose about RNNs.\n"),
        ("Ghost", "ghost", True, None),
    ):
        page = WikiPage(
            id=uuid7(),
            project_id=project_id,
            title=title,
            slug=slug,
            page_kind="stub" if is_stub else "topic",
            is_stub=is_stub,
            status="draft",
            category="architectures",
            page_type="concept",
            tags=[],
            related=[],
            created_by=ACTOR,
        )
        session.add(page)
        await session.flush()
        pages[slug] = page.id
        if body is not None:
            revision = WikiRevision(
                id=uuid7(),
                page_id=page.id,
                project_id=project_id,
                revision_no=1,
                body_md=body,
                summary="",
                author_kind="user",
                author_id=ACTOR,
                body_sha256="0" * 64,
                commit_message="seed",
                ledger_event_id=uuid7(),
            )
            session.add(revision)
            await session.flush()
            page.current_revision_id = revision.id

    live = await _add_claim(
        session,
        project_id=project_id,
        page_id=pages["attention"],
        body=LIVE_CLAIM,
        source_id=source_id,
    )
    await _add_claim(
        session,
        project_id=project_id,
        page_id=pages["attention"],
        body=SUPERSEDED_CLAIM,
        superseded_by=live,
    )
    await _add_claim(session, project_id=project_id, page_id=pages["ghost"], body=STUB_CLAIM)
    await session.commit()


@pytest.fixture
async def seeded(
    maker: async_sessionmaker[AsyncSession], committed_project: uuid.UUID
) -> AsyncIterator[uuid.UUID]:
    async with maker() as session:
        await _seed(session, committed_project)
    yield committed_project
    async with maker() as session:
        # Same reasoning as `test_vault_export_route.py`: `wiki_revisions` is
        # append-only by trigger and is deliberately left alone. The rows are
        # scoped to a throwaway project id.
        await session.execute(
            text("DELETE FROM wiki_pages WHERE project_id = :pid"),
            {"pid": committed_project},
        )
        await session.commit()


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _principal() -> Principal:
    return Principal(user_id=ACTOR, subject="int", email="int@test.local", actor_kind="user")


def _unzip(payload: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


async def test_the_bundle_carries_the_claim_the_quote_and_the_span(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """The whole chain, in the zip, from the real tables.

    Without this the export ships the prose and nothing behind it: the reader
    gets the conclusion and no way to check it, and the vault cannot be
    re-imported as knowledge — only read.
    """
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    assert resp.status_code == 200, resp.text

    files = _unzip(resp.content)
    assert EVIDENCE_FILENAME in files
    page = files["attention.md"]
    assert "## Evidence" in page
    assert LIVE_CLAIM in page
    assert QUOTE in page
    assert f"chars 120-{120 + len(QUOTE)}" in page
    assert "Attention Is All You Need" in page

    document = json.loads(files[EVIDENCE_FILENAME])
    citation = document["pages"][0]["claims"][0]["citations"][0]
    assert citation["quote"] == QUOTE
    assert citation["char_start"] == 120
    assert citation["source_id"] is not None
    assert citation["chunk_id"] is not None


async def test_a_claim_on_a_stub_page_is_not_exported(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """Stubs are excluded from the bundle, so their claims have no file to sit
    next to. A slug in `evidence.json` with no `slug.md` is the dangling
    reference the OKF link rule exists to catch, one level up."""
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    files = _unzip(resp.content)
    document = json.loads(files[EVIDENCE_FILENAME])
    assert STUB_CLAIM not in files[EVIDENCE_FILENAME]
    for page in document["pages"]:
        assert f"{page['slug']}.md" in files, page["slug"]


async def test_a_superseded_claim_is_not_exported(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """Revision is supersession, never mutation, so every earlier wording of
    every belief is still a row. Exporting them ships each draft as though it
    were currently held."""
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    files = _unzip(resp.content)
    assert SUPERSEDED_CLAIM not in files[EVIDENCE_FILENAME]
    assert SUPERSEDED_CLAIM not in files["attention.md"]


async def test_the_counts_separate_cited_from_anchored(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """Two citations, one anchored. A single "citations" number would report
    this corpus as fully evidenced; the live one is 8,079 against 4,082."""
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf&dry_run=true")
    body = resp.json()
    assert body["evidence"] == {
        "included": True,
        "claims": 1,
        "citations": 2,
        "anchored_citations": 1,
        "pages_with_claims": 1,
    }
    assert EVIDENCE_FILENAME in body["files"]


async def test_evidence_false_gives_the_prose_only_vault(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """The flag has to actually remove it, and the report has to say so —
    otherwise "no sidecar" means both "turned off" and "nothing to export"."""
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf&evidence=false")
        dry = await client.post(
            f"/v1/projects/{seeded}/export/vault?dialect=okf&evidence=false&dry_run=true"
        )
    files = _unzip(resp.content)
    assert EVIDENCE_FILENAME not in files
    assert "## Evidence" not in files["attention.md"]
    assert QUOTE not in files["attention.md"]
    assert dry.json()["evidence"]["included"] is False


async def test_the_sidecar_does_not_count_as_a_page(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """`VaultExport.page_count` counts every non-reserved file, so a bundle
    built by merging the sidecar in reports one page too many unless the count
    is taken before the merge. The header and the `.md` files have to agree."""
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    files = _unzip(resp.content)
    concepts = [n for n in files if n.endswith(".md") and n != "index.md"]
    assert resp.headers["x-vault-page-count"] == str(len(concepts)) == "2"
    assert resp.headers["x-vault-claims"] == "1"
    assert resp.headers["x-vault-anchored-citations"] == "1"


async def test_the_ledger_row_records_what_left(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """ "A copy of the wiki left" and "a verbatim quote of somebody else's
    copyrighted text left with it" are different events, and an append-only
    action log that cannot tell them apart cannot answer for either."""
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    assert resp.status_code == 200, resp.text

    async with maker() as session:
        events = (
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == seeded,
                        ActionLedgerEvent.action_kind == "wiki.vault.export",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 1
    payload = events[0].payload_jsonb
    assert payload["evidence_included"] is True
    assert payload["claims"] == 1
    assert payload["citations"] == 2
    assert payload["anchored_citations"] == 1


async def test_two_exports_of_an_unchanged_corpus_are_byte_identical(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """The one property only a real database can check.

    The evidence query joins three tables and fans out over citations; without
    a total ORDER BY, Postgres is free to return them in a different order on
    the second call and the bundle differs run to run. That reads as a format
    bug, is a missing ORDER BY, and makes the round-trip criterion untestable
    against a live corpus.
    """
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        first = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
        second = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    assert first.content == second.content


async def test_a_source_that_was_deleted_still_leaves_its_evidence(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """An inner join here would drop the citation along with the source and
    report the claim as uncited, which is a stronger and false statement."""
    async with maker() as session:
        await session.execute(text("DELETE FROM sources WHERE project_id = :pid"), {"pid": seeded})
        await session.commit()

    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    files = _unzip(resp.content)
    assert QUOTE in files["attention.md"]
    document = json.loads(files[EVIDENCE_FILENAME])
    citation = document["pages"][0]["claims"][0]["citations"][0]
    assert citation["source_title"] is None
    assert citation["source_id"] is not None


async def test_re_exporting_a_body_that_already_has_a_section_does_not_stack(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """A vault edited and pushed back into the wiki carries the section in its
    body. Re-export must replace it, not append a second one — the iterate step
    of this workstream is a worker that re-exports continuously."""
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        first = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    exported = _unzip(first.content)["attention.md"]

    async with maker() as session:
        page_id = (
            await session.execute(
                select(WikiPage.id).where(
                    WikiPage.project_id == seeded, WikiPage.slug == "attention"
                )
            )
        ).scalar_one()
        revision = WikiRevision(
            id=uuid7(),
            page_id=page_id,
            project_id=seeded,
            revision_no=2,
            # The exported file, frontmatter and all, written back as the body.
            body_md=exported,
            summary="",
            author_kind="user",
            author_id=ACTOR,
            body_sha256="1" * 64,
            commit_message="round trip",
            ledger_event_id=uuid7(),
            created_at=utcnow(),
        )
        session.add(revision)
        await session.flush()
        await session.execute(
            text("UPDATE wiki_pages SET current_revision_id = :rid WHERE id = :pid"),
            {"rid": revision.id, "pid": page_id},
        )
        await session.commit()

    async with await _client(app) as client:
        second = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")
    assert _unzip(second.content)["attention.md"].count("## Evidence") == 1


# ---------------------------------------------------------------------------
# The ORDER BY the format's determinism rests on
#
# `test_two_exports_of_an_unchanged_corpus_are_byte_identical` above cannot see
# a missing ORDER BY: two consecutive reads of the same rows in the same
# process come back in the same order whether or not one is asked for.
# Deleting the whole seven-column clause left all ten tests here green, and so
# did replacing the route's `sorted(evidence_by_page.values(), ...)` with
# `reversed(...)`.
#
# What makes it visible is a corpus whose HEAP order is the reverse of its
# sorted order: rows are inserted back to front, and each pair collides on
# every key before the one it exists to pin. Unordered, Postgres hands back
# what it scanned — which is now demonstrably the wrong answer instead of
# accidentally the right one.
#
# Each pair below pins exactly one column:
#
#   WikiPage.slug            zeta inserted before alpha
#   WikiClaim.text           the z claim inserted before the a claim
#   WikiClaim.id             two claims share a text; the higher id first
#   Citation.citation_marker c9 inserted before c1
#   Citation.source_id       same marker, source B inserted before source A
#   Citation.char_start      same marker and source, char 300 inserted first
#                            AND given the lowest id, so dropping char_start
#                            cannot be masked by the id tiebreaker
#   Citation.id              same marker, source and offset; higher id first
# ---------------------------------------------------------------------------

ORDER_CLAIM_A = "aaa — this claim sorts first by text"
ORDER_CLAIM_M = "mmm — two claims on this page share this exact text"
ORDER_CLAIM_Z = "zzz — this claim sorts last by text"
ORDER_CLAIM_ZETA = "000 — the only claim on the page whose slug sorts last"

#: One quote per citation, so the order the export produced is readable off the
#: bundle. Two citations differing only by primary key are otherwise identical
#: in every field the format carries.
ORDER_QUOTES = ("quote A", "quote B", "quote C", "quote D", "quote E")


async def _seed_ordering(session: AsyncSession, project_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """A corpus inserted back to front. Returns the ids it chose, by role."""
    session.add(
        Project(
            id=project_id,
            title="Ordering Export Test",
            description="",
            status="active",
            model_profile_id=uuid7(),
            created_by=ACTOR,
        )
    )
    source_a, source_b = sorted([uuid7(), uuid7()])
    for source_id, title in ((source_a, "Source A"), (source_b, "Source B")):
        session.add(
            Source(
                id=source_id,
                project_id=project_id,
                connector_kind="web",
                external_id=None,
                title=title,
                url=None,
                short_id=f"S{str(source_id)[-6:]}",
                status="ingested",
                created_by=ACTOR,
            )
        )

    pages: dict[str, uuid.UUID] = {}
    # `zeta` FIRST, so heap order is the reverse of slug order.
    for title, slug in (("Zeta", "zeta"), ("Alpha", "alpha")):
        page = WikiPage(
            id=uuid7(),
            project_id=project_id,
            title=title,
            slug=slug,
            page_kind="topic",
            is_stub=False,
            status="draft",
            category="ordering",
            page_type="concept",
            tags=[],
            related=[],
            created_by=ACTOR,
        )
        session.add(page)
        await session.flush()
        pages[slug] = page.id
        revision = WikiRevision(
            id=uuid7(),
            page_id=page.id,
            project_id=project_id,
            revision_no=1,
            body_md=f"Prose about {title}.\n",
            summary="",
            author_kind="user",
            author_id=ACTOR,
            body_sha256="0" * 64,
            commit_message="seed",
            ledger_event_id=uuid7(),
        )
        session.add(revision)
        await session.flush()
        page.current_revision_id = revision.id

    claim_ids = sorted(uuid7() for _ in range(5))
    roles = {
        "m_low": claim_ids[0],
        "m_high": claim_ids[1],
        "a": claim_ids[2],
        "z": claim_ids[3],
        "zeta": claim_ids[4],
    }
    for claim_id, page_slug, body in (
        (roles["z"], "alpha", ORDER_CLAIM_Z),
        (roles["a"], "alpha", ORDER_CLAIM_A),
        (roles["m_high"], "alpha", ORDER_CLAIM_M),
        (roles["m_low"], "alpha", ORDER_CLAIM_M),
        # On the page whose slug sorts LAST, with the text that sorts FIRST: if
        # the query stops ordering by slug this claim leads the whole result.
        (roles["zeta"], "zeta", ORDER_CLAIM_ZETA),
    ):
        session.add(
            WikiClaim(
                id=claim_id,
                project_id=project_id,
                page_id=pages[page_slug],
                text=body,
                claim_key=None,
                superseded_by=None,
                origin="agent",
                evidence_tier="cited",
                confidence="weakly_supported",
                status="active",
                created_by=ACTOR,
            )
        )
    await session.flush()

    citation_ids = sorted(uuid7() for _ in range(5))
    #  role  marker  source    char_start  id             quote
    rows = (
        ("E", "[c9]", source_a, 100, citation_ids[2], ORDER_QUOTES[4]),
        ("D", "[c1]", source_b, 100, citation_ids[1], ORDER_QUOTES[3]),
        ("C", "[c1]", source_a, 300, citation_ids[0], ORDER_QUOTES[2]),
        ("B", "[c1]", source_a, 200, citation_ids[4], ORDER_QUOTES[1]),
        ("A", "[c1]", source_a, 200, citation_ids[3], ORDER_QUOTES[0]),
    )
    for _role, marker, source_id, start, citation_id, quote in rows:
        session.add(
            Citation(
                id=citation_id,
                project_id=project_id,
                claim_id=roles["a"],
                source_id=source_id,
                chunk_id=None,
                quote=quote,
                verbatim=True,
                stance="supports",
                weight=1.0,
                locator_hash=None,
                char_start=start,
                char_end=start + len(quote),
                chunk_ids=[],
                citation_marker=marker,
            )
        )
    await session.commit()
    return roles


@pytest.fixture
async def ordered(
    maker: async_sessionmaker[AsyncSession], second_project: uuid.UUID
) -> AsyncIterator[tuple[uuid.UUID, dict[str, uuid.UUID]]]:
    async with maker() as session:
        roles = await _seed_ordering(session, second_project)
    yield second_project, roles
    async with maker() as session:
        await session.execute(
            text("DELETE FROM wiki_pages WHERE project_id = :pid"), {"pid": second_project}
        )
        await session.commit()


async def test_the_query_returns_pages_claims_and_citations_in_a_total_order(
    maker: async_sessionmaker[AsyncSession],
    ordered: tuple[uuid.UUID, dict[str, uuid.UUID]],
) -> None:
    """Every column of the seven-column ORDER BY, against a corpus stored back
    to front.

    Deleting the whole clause left the ten tests above green — two reads of the
    same rows in one process agree with each other whatever order they are in.
    Here the physical order is the wrong answer, so the query has to sort.
    """
    project_id, roles = ordered
    async with maker() as session:
        evidence = await load_page_evidence(session, project_id)

    assert [page.slug for page in evidence.values()] == ["alpha", "zeta"], (
        "pages come back in whatever order the scan produced"
    )
    alpha = next(page for page in evidence.values() if page.slug == "alpha")
    assert [claim.claim_id for claim in alpha.claims] == [
        str(roles["a"]),
        str(roles["m_low"]),
        str(roles["m_high"]),
        str(roles["z"]),
    ]
    first = alpha.claims[0]
    assert first.text == ORDER_CLAIM_A
    assert [citation.quote for citation in first.citations] == list(ORDER_QUOTES)


async def test_the_sidecar_lists_its_pages_in_slug_order(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    ordered: tuple[uuid.UUID, dict[str, uuid.UUID]],
) -> None:
    """The route's own sort, which is a second ordering and was also unpinned.

    `evidence_by_page` is a dict, so `.values()` is insertion order — the
    query's order, and nobody's contract. Replacing the route's `sorted(...)`
    with `reversed(...)` changed the bytes of every real export and nothing
    went red.
    """
    project_id, _ = ordered
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{project_id}/export/vault?dialect=okf")
    assert resp.status_code == 200, resp.text
    document = json.loads(_unzip(resp.content)[EVIDENCE_FILENAME])
    assert [page["slug"] for page in document["pages"]] == ["alpha", "zeta"]


async def test_the_exported_page_renders_its_citations_in_the_query_order(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    ordered: tuple[uuid.UUID, dict[str, uuid.UUID]],
) -> None:
    """The order reaches the bytes a reader opens, not just the view model.

    Two exports being byte-identical is the criterion; being byte-identical to
    a *stable* arrangement is what makes that criterion mean the format is
    deterministic rather than that the test ran twice quickly.
    """
    project_id, _ = ordered
    app = _build_app(monkeypatch, maker, _principal())
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{project_id}/export/vault?dialect=okf")
    page = _unzip(resp.content)["alpha.md"]
    positions = [page.index(quote) for quote in ORDER_QUOTES]
    assert positions == sorted(positions), page

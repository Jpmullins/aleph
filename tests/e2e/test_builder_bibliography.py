"""E1.3 — an exported artifact must carry the bibliography it cited.

`_node_citation_resolve` walked `[[Source:SHORTID]]` markers out of the composed
markdown, looked each one up, and returned `{"csl_items": [...]}`. `csl_items`
was not declared on `BuilderState`, so LangGraph **discarded the write**. The
next node read `state.get("csl_items") or []`, found nothing, and took its
`if not items: return {"bibliography_markdown": ""}` branch.

Every exported report therefore shipped with an **empty bibliography** while
citing sources in its body — the most damaging possible form of this bug, since
the artifact is the thing that leaves the building. A reader sees citations in
the prose and no references behind them.

`version_no` had the same defect on the same state class, and worse
consequences: LangGraph filters undeclared keys out of the **initial** state
too, so `_node_persist` fell back to `1` and every rebuild silently overwrote
version 1's bytes at the same asset key while the database recorded an
incrementing `version_no`. That one is covered here too, because it is
invisible until someone opens an old version and finds the newest content.

Nothing is faked. The builder makes no LLM calls; the page, its revision, the
source and its CSL metadata are all real rows, and the assertion is on bytes the
real `_node_package` produced.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


#: `sources.short_id` carries a GLOBAL unique constraint, not a per-project
#: one, so each seed mints its own and the body is built to match.
def _page_body(short_id: str) -> str:
    return (
        f"Chain-of-thought prompting elicits reasoning [[Source:{short_id}]].\n\n"
        f"The effect emerges only at scale [[Source:{short_id}]].\n"
    )


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "builder", "email": "b@test.local", "name": "B"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _seed(asgi_app, http_client):
    """A project, a cited Source with CSL metadata, and a page that cites it."""
    from aleph_artifacts.models import Artifact
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_rks.models import Source
    from aleph_security.principal import Principal
    from aleph_wiki.wiki_service import WikiService
    from tests.e2e.test_citation_provenance import _dev_user_id

    resp = await http_client.post(
        "/v1/projects", json={"title": f"builder {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    project_id = UUID(resp.json()["id"])

    maker = asgi_app.state.session_maker
    principal = Principal(
        user_id=(await _dev_user_id(maker)),
        subject="dev@aleph.local",
        email="dev@aleph.local",
        actor_kind="user",
    )

    # Uppercase alnum only: the resolver regex is [A-Z0-9]+.
    short_id = f"COT{uuid4().hex[:5].upper()}"
    async with maker() as session:
        session.add(
            Source(
                id=uuid7(),
                project_id=project_id,
                short_id=short_id,
                title="Chain-of-Thought Prompting Elicits Reasoning",
                url="https://example.invalid/cot",
                connector_kind="upload",
                status="indexed",
                # The fields the CSL mapper reads. Without them the entry
                # renders but carries no author or year to assert on.
                source_metadata_jsonb={
                    "authors": [{"family": "Wei", "given": "Jason"}],
                    "year": 2022,
                    "type": "article-journal",
                },
                created_by=principal.user_id,
                access_scope="project",
            )
        )
        artifact = Artifact(
            id=uuid7(),
            project_id=project_id,
            short_id=f"A{uuid4().hex[:6].upper()}",
            title="A Report",
            artifact_kind="report_markdown_bundle",
            description="",
            created_by=principal.user_id,
            access_scope="project",
        )
        session.add(artifact)
        await session.flush()

        # A real page + revision through the real service, so the builder's
        # outline → section_compose walk has genuine rows to traverse.
        ledger = LedgerWriter(session)
        svc = WikiService(session)
        result = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=project_id,
            page_id=None,
            title="Chain-of-Thought",
            slug=None,
            page_kind="topic",
            body_md=_page_body(short_id),
            summary="CoT.",
            claims=[],
            wikilinks=[],
            commit_message="seed",
            respect_hand_edits=False,
        )
        await session.commit()
        return project_id, artifact.id, result.page_id, principal


def _report_text(asset_store, storage_uri: str) -> str:
    """`report_markdown_bundle` is a ZIP; read the markdown out of it.

    Asserting on the archive bytes would pass or fail for reasons unrelated to
    the bibliography — compressed text contains neither the author nor the year
    as readable strings.
    """
    import io
    import zipfile

    raw = asset_store.get(storage_uri)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        assert "report.md" in names, f"bundle has no report.md, only {names}"
        return z.read("report.md").decode("utf-8", errors="replace")


async def _build(asgi_app, project_id, artifact_id, page_id, principal):
    """Drive the REAL compiled builder graph."""
    from aleph_artifacts.builder.workflow import BuilderWorkflow
    from aleph_artifacts.models import Artifact
    from aleph_db.repos.ledger import LedgerWriter

    maker = asgi_app.state.session_maker
    workflow = BuilderWorkflow(
        session_maker=maker,
        asset_store=asgi_app.state.asset_store,
        principal=principal,
    )
    async with maker() as session:
        artifact = await session.get(Artifact, artifact_id)
        assert artifact is not None
        ledger = LedgerWriter(session)
        version = await workflow.run(
            ledger=ledger,
            project_id=project_id,
            agent_run_id=uuid7(),
            artifact=artifact,
            artifact_kind="report_markdown_bundle",
            wiki_page_ids=[page_id],
            dataset_version_ids=[],
            template_name="default",
            csl_style="apa-7",
        )
        await session.commit()
        return version


class TestBibliographySurvivesTheGraph:
    async def test_exported_artifact_has_a_bibliography(self, asgi_app, http_client, auth_bypass):
        """The headline: a body that cites a source must ship its reference."""
        project_id, artifact_id, page_id, principal = await _seed(asgi_app, http_client)
        version = await _build(asgi_app, project_id, artifact_id, page_id, principal)

        text = _report_text(asgi_app.state.asset_store, version.storage_uri)

        assert "(no citations)" not in text, (
            "the artifact reports it has no citations while its body cites "
            "[[Source:COT1]] — `csl_items` was dropped between citation_resolve "
            "and bibliography, so every export ships an empty reference list"
        )
        assert "Wei" in text, f"the bibliography carries no author. Rendered:\n{text[:600]}"
        assert "2022" in text, f"the bibliography carries no year. Rendered:\n{text[:600]}"

    async def test_a_body_citing_nothing_gets_no_invented_references(
        self, asgi_app, http_client, auth_bypass
    ):
        """Empty must mean "nothing was cited", not "the write was lost".

        Without this the headline test could pass against a builder that lists
        every source in the project regardless of what the text cites.
        """
        from aleph_db.repos.ledger import LedgerWriter
        from aleph_wiki.wiki_service import WikiService

        project_id, artifact_id, _page_id, principal = await _seed(asgi_app, http_client)
        async with asgi_app.state.session_maker() as session:
            result = await WikiService(session).commit_revision(
                principal=principal,
                ledger=LedgerWriter(session),
                project_id=project_id,
                page_id=None,
                title="Uncited",
                slug=None,
                page_kind="topic",
                body_md="Nothing is cited here.\n",
                summary="",
                claims=[],
                wikilinks=[],
                commit_message="seed2",
                respect_hand_edits=False,
            )
            await session.commit()
        version = await _build(asgi_app, project_id, artifact_id, result.page_id, principal)
        text = _report_text(asgi_app.state.asset_store, version.storage_uri)
        assert "Wei" not in text, "a source the text never cites appeared in the references"


class TestVersionBytesAreNotOverwritten:
    """`version_no` was dropped from the INITIAL state, not a node write.

    LangGraph filters undeclared keys out of the initial state too, so
    `_node_persist` fell back to `1` and every rebuild wrote over version 1's
    bytes at the same asset key — while the database dutifully recorded 2, 3, 4.
    Opening an old version returned the newest content, silently.
    """

    async def test_each_rebuild_writes_a_distinct_asset(self, asgi_app, http_client, auth_bypass):
        from aleph_artifacts.models import ArtifactVersion

        project_id, artifact_id, page_id, principal = await _seed(asgi_app, http_client)
        first = await _build(asgi_app, project_id, artifact_id, page_id, principal)
        second = await _build(asgi_app, project_id, artifact_id, page_id, principal)

        assert second.version_no == first.version_no + 1
        assert second.storage_uri != first.storage_uri, (
            f"both versions point at {first.storage_uri} — the rebuild "
            f"overwrote the earlier version's bytes while the row recorded a "
            f"new version_no, so version history is fiction"
        )

        async with asgi_app.state.session_maker() as session:
            rows = list(
                (
                    await session.execute(
                        select(ArtifactVersion)
                        .where(ArtifactVersion.artifact_id == artifact_id)
                        .order_by(ArtifactVersion.version_no)
                    )
                )
                .scalars()
                .all()
            )
        assert [r.version_no for r in rows] == [1, 2]
        assert len({r.storage_uri for r in rows}) == 2

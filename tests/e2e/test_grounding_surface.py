"""The grounding surface must carry a real chain, or say plainly that it cannot.

This surface is the platform's claim about itself made checkable: claim →
citation → chunk → character span → text a reader can go and verify.

Every hop existed in the schema long before any of them carried data.
`Citation.source_page_id` was NULL at every production write site, `chunk_ids`
was `[]`, and the chunker's offsets did not index their own text (35 of 44
chunks of this repo's own CLAUDE.md were wrong). An inspector built on that
would have rendered an authoritative, confident, **empty** chain — worse than
no inspector, because it tells an analyst the claim was checked.

So these tests are mostly about the negative space:

* a claim whose citation resolves must produce the *source*, not just a row;
* a chunk that reaches the surface must have offsets that still slice the
  source markdown exactly — the property that makes the quote checkable;
* an ungrounded claim must render as ungrounded, not as an error and not as
  silence.

Nothing on the resolving path is hand-built where production builds it: the
source-page/citation link comes from driving the real commit node, and chunk
offsets come from the real chunker over real markdown.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select

from aleph_core.ids import uuid7

pytestmark = pytest.mark.integration

MARKDOWN = """# Chain-of-Thought Prompting

On GSM8K, a math word problem benchmark, chain-of-thought prompting with PaLM
540B achieves a 56.9% solve rate, up from 17.9% with standard prompting.

## Limitations

Chain-of-thought improves accuracy but does not eliminate hallucination.
"""


def _data_model(messages: list[dict]) -> dict:
    """The bulk `updateDataModel` payload — what the renderer actually binds."""
    for m in messages:
        if m.get("updateDataModel") or m.get("kind") == "updateDataModel":
            payload = m.get("updateDataModel", m)
            if isinstance(payload, dict) and "value" in payload:
                return payload["value"]
    for m in reversed(messages):
        for v in m.values():
            if isinstance(v, dict) and "value" in v and isinstance(v["value"], dict):
                return v["value"]
    raise AssertionError(f"no updateDataModel in {messages!r}")


async def _grounding(session, project_id, claim_id):
    from aleph_api.routes.surfaces import _grounding_messages

    return _data_model(await _grounding_messages(session, project_id, claim_id, "grounding"))


class TestUnresolvableInputs:
    """A surface that cannot answer must say so, not 500 and not pretend."""

    async def test_no_claim_selected(self, asgi_app):
        async with asgi_app.state.session_maker() as session:
            dm = await _grounding(session, uuid7(), None)
        assert dm == {"claim": None, "groundings": []}

    async def test_malformed_claim_id(self, asgi_app):
        async with asgi_app.state.session_maker() as session:
            dm = await _grounding(session, uuid7(), "not-a-uuid")
        assert dm["claim"] is None

    async def test_claim_from_another_project_is_not_disclosed(self, asgi_app):
        """Project scoping is enforced in the query, not by the caller."""
        from aleph_security.principal import Principal
        from tests.e2e.test_citation_provenance import (
            _dev_user_id,
            _run_commit_node,
            _seed_project_and_source,
        )

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

        from aleph_wiki.models import WikiClaim

        async with maker() as session:
            claim = (
                (await session.execute(select(WikiClaim).where(WikiClaim.project_id == project_id)))
                .scalars()
                .first()
            )
            assert claim is not None
            # Same claim id, a different project's scope.
            dm = await _grounding(session, uuid7(), str(claim.id))
        assert dm["claim"] is None, "a claim leaked across a project boundary"


class TestRealChain:
    async def test_surface_reports_the_source_a_claim_rests_on(self, asgi_app):
        """Drives the production commit node — constructs nothing on the chain."""
        from aleph_security.principal import Principal
        from aleph_wiki.models import WikiClaim
        from tests.e2e.test_citation_provenance import (
            _dev_user_id,
            _run_commit_node,
            _seed_project_and_source,
        )

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
            claim = (
                (await session.execute(select(WikiClaim).where(WikiClaim.project_id == project_id)))
                .scalars()
                .first()
            )
            assert claim is not None
            dm = await _grounding(session, project_id, str(claim.id))

        assert dm["claim"]["text"] == "The effect holds."
        assert dm["claim"]["confidence"] == "cited"
        assert dm["groundings"], (
            "the claim rendered with no citations at all, after the production "
            "commit node ran — the surface would tell an analyst a cited claim "
            "is unsupported"
        )
        g = dm["groundings"][0]
        assert g["source"] is not None, (
            "citation resolved to no source. This is the NULL `source_page_id` "
            "defect: the row exists, the join returns nothing, and the panel "
            "renders an authoritative empty chain."
        )
        assert g["source"]["title"] == source.title
        assert g["source"]["retracted"] is False


class TestChunkHop:
    """The hop that makes a quote checkable rather than merely attributed."""

    async def _seed_chunked_claim(self, asgi_app):
        """A real chain, with chunk offsets from the real chunker.

        The rows are seeded rather than produced by the ingest worker (that path
        needs an LLM and an object store), but the *value under test* — the
        char offsets — comes from `chunk_markdown` over `MARKDOWN`, never from
        numbers chosen to make the assertion pass.
        """
        from sqlalchemy import func

        from aleph_core.time import utcnow
        from aleph_db.models.model_profile import ModelProfile
        from aleph_db.repos import project as project_repo
        from aleph_rks.chunking import chunk_markdown
        from aleph_rks.models import (
            EMBEDDING_DIM,
            DocumentChunk,
            NormalizedDocument,
            Source,
            SourceVersion,
        )
        from aleph_wiki.models import Citation, SourcePage, WikiClaim, WikiPage
        from tests.e2e.test_citation_provenance import _dev_user_id

        maker = asgi_app.state.session_maker
        user_id = await _dev_user_id(maker)
        async with maker() as session:
            profile = ModelProfile(
                id=uuid7(),
                project_id=None,
                name=f"g-{uuid4().hex[:8]}",
                is_template=False,
                bindings_jsonb={},
                created_by=user_id,
                access_scope="project",
            )
            session.add(profile)
            await session.flush()
            project = project_repo.new_project(
                title="Grounding",
                description="",
                model_profile_id=profile.id,
                created_by=user_id,
            )
            session.add(project)
            await session.flush()

            source = Source(
                id=uuid7(),
                project_id=project.id,
                short_id=f"S{uuid4().hex[:4].upper()}",
                title="CoT Paper",
                url="https://example.invalid/cot",
                connector_kind="upload",
                status="normalized",
                source_metadata_jsonb={},
                created_by=user_id,
                access_scope="project",
            )
            session.add(source)
            await session.flush()

            version = SourceVersion(
                id=uuid7(),
                source_id=source.id,
                version_no=1,
                asset_id=uuid7(),
                sha256="0" * 64,
                fetched_at=utcnow(),
                created_by=user_id,
                access_scope="project",
            )
            session.add(version)
            await session.flush()

            normalized = NormalizedDocument(
                id=uuid7(),
                project_id=project.id,
                source_id=source.id,
                source_version_id=version.id,
                markdown_uri="fs://test/cot.md",
                parser="test",
                parser_version="1",
                char_count=len(MARKDOWN),
                token_count=len(MARKDOWN.split()),
                structure_jsonb={},
                quality_flags_jsonb=[],
                created_by=user_id,
                access_scope="project",
            )
            session.add(normalized)
            await session.flush()

            # REAL chunker over REAL markdown — the offsets under test are the
            # ones production computes.
            chunks = chunk_markdown(MARKDOWN, target_tokens=64)
            assert chunks
            rows = [
                DocumentChunk(
                    id=uuid7(),
                    project_id=project.id,
                    source_id=source.id,
                    normalized_document_id=normalized.id,
                    ordinal=c.ordinal,
                    text=c.text,
                    text_tsv=func.to_tsvector("english", c.text),
                    embedding=[0.0] * EMBEDDING_DIM,
                    section_path=c.section_path,
                    char_start=c.char_start,
                    char_end=c.char_end,
                    token_count=c.token_count,
                    embedder_model="test-embed",
                )
                for c in chunks
            ]
            session.add_all(rows)
            await session.flush()

            page = WikiPage(
                id=uuid7(),
                project_id=project.id,
                title="Chain-of-Thought",
                slug=f"cot-{uuid4().hex[:6]}",
                page_kind="topic",
                status="published",
                created_by=user_id,
                access_scope="project",
            )
            session.add(page)
            await session.flush()

            session.add(
                SourcePage(
                    id=uuid7(),
                    project_id=project.id,
                    source_id=source.id,
                    page_id=page.id,
                    extracted_claims_jsonb=[],
                    extracted_at=utcnow(),
                )
            )
            revision_id = uuid7()
            claim = WikiClaim(
                id=uuid7(),
                project_id=project.id,
                page_id=page.id,
                revision_id=revision_id,
                text="CoT reaches 56.9% on GSM8K with PaLM 540B.",
                confidence="cited",
                section_anchor="s",
                created_by=user_id,
                access_scope="project",
            )
            session.add(claim)
            await session.flush()

            source_page_id = (
                await session.execute(
                    select(SourcePage.id).where(SourcePage.source_id == source.id)
                )
            ).scalar_one()
            session.add(
                Citation(
                    id=uuid7(),
                    project_id=project.id,
                    claim_id=claim.id,
                    source_page_id=source_page_id,
                    citation_marker="[c1]",
                    chunk_ids=[str(rows[0].id)],
                )
            )
            await session.commit()
            return project.id, claim.id

    async def test_chunks_reach_the_surface_with_usable_offsets(self, asgi_app):
        """The payoff: the quote shown must slice the source document exactly.

        This is what separates a citation from a checkable one. If the offsets
        drift, the surface highlights confidently and wrongly.
        """
        project_id, claim_id = await self._seed_chunked_claim(asgi_app)
        async with asgi_app.state.session_maker() as session:
            dm = await _grounding(session, project_id, str(claim_id))

        chunks = dm["groundings"][0]["chunks"]
        assert chunks, (
            "the citation reached the surface carrying no chunks — `chunk_ids` "
            "is the hop that makes a claim checkable, and an empty list renders "
            "as 'attributed but unquotable'"
        )
        for c in chunks:
            assert MARKDOWN[c["char_start"] : c["char_end"]] == c["text"], (
                f"chunk {c['id']} offsets [{c['char_start']}:{c['char_end']}] do "
                f"not select its own text — a reader clicking through would be "
                f"shown the wrong span"
            )
        assert "GSM8K" in " ".join(c["text"] for c in chunks)

    async def test_ungrounded_claim_is_reported_not_hidden(self, asgi_app):
        """An unsupported claim is the most important thing this surface says."""
        from aleph_wiki.models import Citation

        project_id, claim_id = await self._seed_chunked_claim(asgi_app)
        async with asgi_app.state.session_maker() as session:
            cite = (
                await session.execute(select(Citation).where(Citation.claim_id == claim_id))
            ).scalar_one()
            cite.chunk_ids = []
            await session.commit()

        async with asgi_app.state.session_maker() as session:
            dm = await _grounding(session, project_id, str(claim_id))

        g = dm["groundings"][0]
        assert g["chunks"] == []
        assert g["source"] is not None, (
            "losing the chunk hop must not also lose the source hop — they are "
            "independent, and conflating them hides which one broke"
        )

    async def test_claim_with_no_citations_renders_as_such(self, asgi_app):
        from aleph_wiki.models import Citation

        project_id, claim_id = await self._seed_chunked_claim(asgi_app)
        async with asgi_app.state.session_maker() as session:
            cite = (
                await session.execute(select(Citation).where(Citation.claim_id == claim_id))
            ).scalar_one()
            await session.delete(cite)
            await session.commit()

        async with asgi_app.state.session_maker() as session:
            dm = await _grounding(session, project_id, str(claim_id))

        assert dm["claim"] is not None, "the claim itself must still render"
        assert dm["groundings"] == []


async def test_surface_is_addressable_as_a_pane(asgi_app):
    """`grounding` must be a real pane kind, or the surface is unreachable."""
    from aleph_api.routes.surfaces import _PANE_KINDS, _parse_pane_specs

    assert "grounding" in _PANE_KINDS
    specs = _parse_pane_specs("wiki,grounding:page_id=abc")
    assert any(tab == "grounding" for _sid, tab, _pid in specs)


def test_renderer_binds_only_what_the_builder_emits() -> None:
    """Renderer props and builder data model must not drift apart.

    A renderer reading a key the builder never sets renders blank forever, and
    nothing else in the stack would notice.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    builder = (root / "packages/aleph-a2ui/src/aleph_a2ui/components/surfaces.py").read_text()
    renderer = (root / "apps/web/src/a2ui/components/GroundingSurface.tsx").read_text()

    assert '"component": "GroundingSurface"' in builder
    for key in ("claim", "groundings"):
        assert f'"{key}": {{"path": "/{key}"}}' in builder, f"builder stopped binding {key}"
        assert f"{key}?:" in renderer, f"renderer stopped reading {key}"
    for field in ("char_start", "char_end", "section_path", "retracted", "marker"):
        assert field in renderer, f"renderer dropped {field}, which the builder emits"

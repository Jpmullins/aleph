"""CuratorService: back-resolves broken wikilinks after a page exists.

Integration (real DB). Reproduces the bug that motivated the curator: an
overview page links to a topic page that is created later; the link is born
broken (``dst_page_id IS NULL``) and nothing ever repaired it on the
research/bootstrap path. Running the curator for the new page repairs the
overview link and registers the page's title alias.

See ``docs/superpowers/specs/2026-06-25-wiki-curator-design.md``.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "user-curator", "email": "curator@test.local", "name": "Curator"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def test_curator_repairs_overview_link_to_existing_page(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import Alias, WikiLink, WikiPage

    # Don't auto-trigger bootstrap research on project create — this test only
    # exercises the curator against pages it seeds directly.
    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)

    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Curator Repair", "description": "x"},
    )
    assert proj.status_code == 201, proj.text
    pid = UUID(proj.json()["id"])

    maker = asgi_app.state.session_maker

    # Seed: an overview page with a broken link to a topic page that already
    # exists with an exact-matching title (the resolver should connect them).
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        overview = WikiPage(
            id=uuid7(),
            project_id=pid,
            title="Curator Repair Overview",
            slug="curator-repair-overview",
            page_kind="topic",
            status="draft",
            created_by=owner,
        )
        topic = WikiPage(
            id=uuid7(),
            project_id=pid,
            title="Curator Repair Topic X",
            slug="curator-repair-topic-x",
            page_kind="topic",
            status="approved",
            created_by=owner,
        )
        session.add_all([overview, topic])
        await session.flush()
        topic_id = topic.id
        link = WikiLink(
            id=uuid7(),
            project_id=pid,
            src_page_id=overview.id,
            src_revision_id=uuid7(),
            dst_page_id=None,
            dst_title="Curator Repair Topic X",
            occurrences=1,
        )
        session.add(link)
        await session.commit()
        link_id = link.id

    # Pre-condition: the link is broken.
    async with maker() as session:
        broken = (
            await session.execute(select(WikiLink).where(WikiLink.id == link_id))
        ).scalar_one()
        assert broken.dst_page_id is None

    # Run the curator for the newly-existing topic page.
    async with maker() as session:
        result = await CuratorService(session).curate(project_id=pid, page_id=topic_id)
        await session.commit()

    assert result.links_repaired >= 1
    assert result.aliases_registered == 1

    # Post-condition: the overview link now resolves to the topic page, and the
    # page's title alias is registered.
    async with maker() as session:
        repaired = (
            await session.execute(select(WikiLink).where(WikiLink.id == link_id))
        ).scalar_one()
        assert repaired.dst_page_id == topic_id

        alias = (
            await session.execute(
                select(Alias).where(
                    Alias.project_id == pid,
                    Alias.surface_form == "Curator Repair Topic X",
                )
            )
        ).scalar_one()
        assert alias.canonical_page_id == topic_id

    # Idempotent: a second curate run repairs nothing new.
    async with maker() as session:
        again = await CuratorService(session).curate(project_id=pid, page_id=topic_id)
        await session.commit()
    assert again.links_repaired == 0


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeLLM:
    """Duck-typed stand-in for LiteLLMClient.chat (curator only reads .choices)."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def chat(self, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse(self._content)


async def test_curator_recurates_overview_with_new_topic(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import WikiLink, WikiPage
    from aleph_wiki.wiki_service import WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)

    title = "Curator Overview Project"
    proj = await http_client.post("/v1/projects", json={"title": title, "description": "x"})
    assert proj.status_code == 201, proj.text
    pid = UUID(proj.json()["id"])

    maker = asgi_app.state.session_maker

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)
        # Overview page: title MUST equal the project title (how the curator
        # finds the overview). No link to the topic yet.
        await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title=title,
            slug=None,
            page_kind="topic",
            body_md="# Overview\n\nResearch overview for this project.\n",
            summary="overview",
            claims=[],
            wikilinks=[],
            commit_message="seed overview",
        )
        topic = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Curator Topic Z",
            slug=None,
            page_kind="topic",
            body_md="# Curator Topic Z\n\nDetails about Z.\n",
            summary="A topic about Z.",
            claims=[],
            wikilinks=[],
            commit_message="seed topic",
        )
        await session.commit()
        topic_id = topic.page_id

    fake_overview = (
        "# Overview\n\nResearch overview for this project.\n\n"
        "## Topics\n\n- [[Curator Topic Z]] — covers Z.\n"
    )
    fake_llm = _FakeLLM(fake_overview)

    async with maker() as session:
        recurated = await CuratorService(session).recurate_overview(
            project_id=pid,
            new_page_id=topic_id,
            litellm=fake_llm,  # type: ignore[arg-type]
            profile_bindings={},
            principal=Principal(user_id=owner, subject="agent", email="", actor_kind="aleph_agent"),
            agent_run_id=uuid7(),
        )
        await session.commit()

    assert recurated is True

    # The overview now contains the wikilink, and it resolves to the topic page.
    async with maker() as session:
        overview = (
            await session.execute(
                select(WikiPage).where(WikiPage.project_id == pid, WikiPage.title == title)
            )
        ).scalar_one()
        from aleph_wiki.models import WikiRevision

        rev = (
            await session.execute(
                select(WikiRevision).where(WikiRevision.id == overview.current_revision_id)
            )
        ).scalar_one()
        assert "[[Curator Topic Z]]" in rev.body_md

        link = (
            await session.execute(
                select(WikiLink).where(
                    WikiLink.src_page_id == overview.id,
                    WikiLink.dst_title == "Curator Topic Z",
                )
            )
        ).scalar_one()
        assert link.dst_page_id == topic_id


async def test_curator_dedup_detect_creates_merge_proposal(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import PageMergeProposal
    from aleph_wiki.wiki_service import WikiService

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)

    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Dedup Test Project", "description": "x"},
    )
    assert proj.status_code == 201, proj.text
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        principal = Principal(user_id=owner, subject="seed", email="", actor_kind="user")
        svc = WikiService(session)
        ledger = LedgerWriter(session)
        # Two clearly-redundant pages (shared distinctive terms -> full-text match).
        await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Knowledge Distillation Methods",
            slug=None,
            page_kind="topic",
            body_md="# Knowledge Distillation Methods\n\nKnowledge distillation transfers "
            "knowledge from a teacher model to a student model.\n",
            summary="Knowledge distillation transfers knowledge from teacher to student models.",
            claims=[],
            wikilinks=[],
            commit_message="seed existing",
        )
        new = await svc.commit_revision(
            principal=principal,
            ledger=ledger,
            project_id=pid,
            page_id=None,
            title="Knowledge Distillation Techniques",
            slug=None,
            page_kind="topic",
            body_md="# Knowledge Distillation Techniques\n\nKnowledge distillation techniques "
            "transfer knowledge from a teacher model to a student model.\n",
            summary="Knowledge distillation techniques transfer knowledge from teacher to student.",
            claims=[],
            wikilinks=[],
            commit_message="seed new",
        )
        await session.commit()
        new_page_id = new.page_id

    fake_llm = _FakeLLM("YES: both pages cover knowledge distillation and are redundant")

    async with maker() as session:
        proposal_id = await CuratorService(session).dedup_detect(
            project_id=pid,
            page_id=new_page_id,
            litellm=fake_llm,  # type: ignore[arg-type]
            profile_bindings={},
            principal=Principal(user_id=owner, subject="agent", email="", actor_kind="aleph_agent"),
            agent_run_id=uuid7(),
        )
        await session.commit()

    assert proposal_id is not None
    async with maker() as session:
        prop = (
            await session.execute(
                select(PageMergeProposal).where(PageMergeProposal.id == proposal_id)
            )
        ).scalar_one()
        assert prop.source_page_id == new_page_id
        assert prop.status == "pending"
        assert prop.similarity > 0


async def test_curator_apply_merge_redirects_and_soft_deletes(
    http_client, auth_bypass, asgi_app, monkeypatch
):
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_security.principal import Principal
    from aleph_wiki.curator_service import CuratorService
    from aleph_wiki.models import Alias, PageMergeProposal, WikiLink, WikiPage

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)

    proj = await http_client.post(
        "/v1/projects",
        json={"title": "Merge Test Project", "description": "x"},
    )
    assert proj.status_code == 201, proj.text
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()
        source = WikiPage(
            id=uuid7(),
            project_id=pid,
            title="Merge Source",
            slug="merge-source",
            page_kind="topic",
            status="draft",
            created_by=owner,
        )
        target = WikiPage(
            id=uuid7(),
            project_id=pid,
            title="Merge Target",
            slug="merge-target",
            page_kind="topic",
            status="approved",
            created_by=owner,
        )
        other = WikiPage(
            id=uuid7(),
            project_id=pid,
            title="Other Page",
            slug="other-page",
            page_kind="topic",
            status="approved",
            created_by=owner,
        )
        session.add_all([source, target, other])
        await session.flush()
        # A link from `other` pointing at the source — should redirect to target.
        link = WikiLink(
            id=uuid7(),
            project_id=pid,
            src_page_id=other.id,
            src_revision_id=uuid7(),
            dst_page_id=source.id,
            dst_title="Merge Source",
            occurrences=1,
        )
        proposal = PageMergeProposal(
            id=uuid7(),
            project_id=pid,
            source_page_id=source.id,
            target_page_id=target.id,
            rationale="redundant",
            similarity=0.5,
            status="pending",
            created_by=owner,
        )
        session.add_all([link, proposal])
        await session.commit()
        source_id, target_id, link_id, prop_id = source.id, target.id, link.id, proposal.id

    async with maker() as session:
        principal = Principal(user_id=owner, subject="agent", email="", actor_kind="aleph_agent")
        redirected = await CuratorService(session).apply_merge(
            proposal=(
                await session.execute(
                    select(PageMergeProposal).where(PageMergeProposal.id == prop_id)
                )
            ).scalar_one(),
            principal=principal,
            ledger=LedgerWriter(session),
        )
        await session.commit()

    assert redirected == 1
    async with maker() as session:
        link = (await session.execute(select(WikiLink).where(WikiLink.id == link_id))).scalar_one()
        assert link.dst_page_id == target_id  # redirected
        src = (await session.execute(select(WikiPage).where(WikiPage.id == source_id))).scalar_one()
        assert src.status == "deleted"  # soft-deleted
        alias = (
            await session.execute(
                select(Alias).where(Alias.project_id == pid, Alias.surface_form == "Merge Source")
            )
        ).scalar_one()
        assert alias.canonical_page_id == target_id  # [[Merge Source]] -> target

"""`POST /v1/projects/{id}/export/vault` against a real Postgres (WS-H8).

The pure-format half of the exporter is covered without a database in
`packages/aleph-artifacts/tests/test_vault_export.py`. What needs Postgres is
the half that decides *what gets exported*: the stub filter, the outer join to
the current revision, the aliases join, and the ledger row. All four are the
kind of thing a mocked session reports as working while the real query returns
the wrong set — a stub filter that silently matched nothing would still produce
a zip, and the criterion ("one .md per non-stub page plus index.md") would pass
against a bundle containing every red link in the corpus.

The app is built without its lifespan; only `session_maker`, `settings` and the
principal seam are supplied. The session is real and the writes really commit,
because the route opens its own session and the ledger row has to survive it.
"""

from __future__ import annotations

import io
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
from aleph_db.models.ledger import ActionLedgerEvent
from aleph_db.models.project import Project
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole
from aleph_wiki.models import WikiPage, WikiRevision

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000bb")


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


async def _seed(session: AsyncSession, project_id: uuid.UUID) -> None:
    """Two real pages, one stub, and one real page with no revision at all.

    The bodyless page is not a contrivance: the live corpus has exactly one, and
    it is the row that decides whether the file count matches
    `count(*) where not is_stub` or is quietly one short.
    """
    # `committed_project` yields an id and creates no row, so the project the
    # export names its bundle after has to be created here.
    session.add(
        Project(
            id=project_id,
            title="Vault Export Test",
            description="",
            status="active",
            model_profile_id=uuid7(),
            created_by=ACTOR,
        )
    )
    pages = [
        ("Alpha", "alpha", False, "Alpha links to [[beta]].\n"),
        ("Beta", "beta", False, "Beta is linked from [[Alpha]].\n"),
        ("Gamma", "gamma", True, None),  # stub — must not be exported
        ("Delta", "delta", False, None),  # non-stub, no revision
    ]
    for title, slug, is_stub, body in pages:
        page = WikiPage(
            id=uuid7(),
            project_id=project_id,
            title=title,
            slug=slug,
            page_kind="stub" if is_stub else "topic",
            is_stub=is_stub,
            status="draft",
            category="architectures",
            page_type="concept" if title != "Delta" else None,
            tags=["architecture"],
            related=[],
            confidence="high",
            created_by=ACTOR,
        )
        session.add(page)
        await session.flush()
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
    await session.commit()


@pytest.fixture
async def seeded(
    maker: async_sessionmaker[AsyncSession], committed_project: uuid.UUID
) -> AsyncIterator[uuid.UUID]:
    async with maker() as session:
        await _seed(session, committed_project)
    yield committed_project
    async with maker() as session:
        # `wiki_revisions` is deliberately NOT deleted: it carries an
        # append-only trigger, and `aleph` is a superuser in the compose stack,
        # so a teardown *could* bypass it. It must not — a fixture that
        # switches off a core invariant to tidy up is how the invariant stops
        # being one. Same reasoning as the `action_ledger_events` note in
        # tests/integration/conftest.py. The rows are scoped to a throwaway
        # project id and interfere with nothing.
        await session.execute(
            text("DELETE FROM wiki_pages WHERE project_id = :pid"),
            {"pid": committed_project},
        )
        await session.commit()


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def test_export_returns_a_zip_of_one_file_per_non_stub_page(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    principal = Principal(user_id=ACTOR, subject="int", email="int@test.local", actor_kind="user")
    app = _build_app(monkeypatch, maker, principal)

    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = sorted(zf.namelist())
    # Three non-stub pages plus the index. `gamma` is a stub and must be absent:
    # 779 of the 844 live pages are stubs, and exporting them produces a vault
    # that is 92% empty files.
    assert names == ["alpha.md", "beta.md", "delta.md", "index.md"]
    assert resp.headers["x-vault-page-count"] == "3"


async def test_export_writes_a_ledger_row(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """A full-corpus export is data egress; the action log has to record it."""
    principal = Principal(user_id=ACTOR, subject="int", email="int@test.local", actor_kind="user")
    app = _build_app(monkeypatch, maker, principal)

    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=obsidian")
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
    assert events[0].payload_jsonb["dialect"] == "obsidian"
    assert events[0].payload_jsonb["page_count"] == 3


async def test_dry_run_reports_the_files_and_the_dangling_links(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    """Without this the dangling report has no reader, and the exporter's one
    non-obvious behaviour — an unresolvable link is unlinked, not emitted — is
    invisible to every caller that only downloads the zip."""
    principal = Principal(user_id=ACTOR, subject="int", email="int@test.local", actor_kind="user")
    app = _build_app(monkeypatch, maker, principal)

    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=okf&dry_run=true")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dialect"] == "okf"
    assert body["page_count"] == 3
    assert body["files"] == ["alpha.md", "beta.md", "delta.md", "index.md"]
    assert body["dangling"] == []

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
    assert events == []


async def test_an_unknown_dialect_is_a_422(
    monkeypatch: pytest.MonkeyPatch,
    maker: async_sessionmaker[AsyncSession],
    seeded: uuid.UUID,
) -> None:
    principal = Principal(user_id=ACTOR, subject="int", email="int@test.local", actor_kind="user")
    app = _build_app(monkeypatch, maker, principal)
    async with await _client(app) as client:
        resp = await client.post(f"/v1/projects/{seeded}/export/vault?dialect=latex")
    assert resp.status_code == 422, resp.text

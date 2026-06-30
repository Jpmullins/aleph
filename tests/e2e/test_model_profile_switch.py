"""Model-profile switch route + reembed_for_project (audit F17/F18)."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import func, select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_switch_profile_route_updates_and_ledgers(http_client, asgi_app, monkeypatch):
    from aleph_db.models.ledger import ActionLedgerEvent

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post("/v1/projects", json={"title": "ProfSwitch", "description": "x"})
    pid = proj.json()["id"]

    cur = await http_client.get(f"/v1/projects/{pid}/model-profile")
    assert cur.status_code == 200
    start_name = cur.json()["name"]
    target = "aleph-production" if start_name != "aleph-production" else "aleph-dev"

    resp = await http_client.post(
        f"/v1/projects/{pid}/model-profile/switch", json={"profile_name": target}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == target

    maker = asgi_app.state.session_maker
    async with maker() as session:
        n = (
            await session.execute(
                select(func.count())
                .select_from(ActionLedgerEvent)
                .where(
                    ActionLedgerEvent.project_id == UUID(pid),
                    ActionLedgerEvent.action_kind == "model_profile.switch",
                )
            )
        ).scalar_one()
        assert n == 1

    v = await http_client.get(f"/v1/projects/{pid}/ledger/verify")
    assert v.json()["ok"] is True


class _FakeEmbedResp:
    def __init__(self, n: int) -> None:
        self.embeddings = [[0.1] * 1024 for _ in range(n)]
        self.input_tokens = 10
        self.cost_usd = "0"
        self.model = "new-embed-model"


class _FakeEmbedClient:
    async def embed(self, *, input, **_kw):
        return _FakeEmbedResp(len(list(input)))


async def test_reembed_for_project_touches_only_stale(http_client, asgi_app, monkeypatch):
    from aleph_core.ids import uuid7
    from aleph_db.models.project import Project
    from aleph_rks.models import DocumentChunk, RetrievalIndexRecord
    from aleph_rks.retrieval import reembed_for_project
    from aleph_security.principal import Principal

    monkeypatch.setattr(asgi_app.state.settings, "bootstrap_auto_enabled", False)
    proj = await http_client.post("/v1/projects", json={"title": "Reembed", "description": "x"})
    pid = UUID(proj.json()["id"])
    maker = asgi_app.state.session_maker

    stale_src, fresh_src = uuid7(), uuid7()
    async with maker() as session:
        owner = (
            await session.execute(select(Project.created_by).where(Project.id == pid))
        ).scalar_one()

        def _chunk(src, ordinal, model):
            return DocumentChunk(
                id=uuid7(),
                project_id=pid,
                source_id=src,
                normalized_document_id=uuid7(),
                ordinal=ordinal,
                text=f"chunk {ordinal}",
                text_tsv=func.to_tsvector("english", f"chunk {ordinal}"),
                embedding=[0.0] * 1024,
                char_start=0,
                char_end=7,
                token_count=2,
                embedder_model=model,
            )

        def _rec(src, model):
            from aleph_core.time import utcnow

            return RetrievalIndexRecord(
                id=uuid7(),
                project_id=pid,
                source_id=src,
                embedder_model=model,
                chunk_count=1,
                indexed_at=utcnow(),
                created_by=owner,
                access_scope="project",
            )

        session.add_all(
            [
                _chunk(stale_src, 0, "old-embed-model"),
                _rec(stale_src, "old-embed-model"),
                _chunk(fresh_src, 0, "new-embed-model"),
                _rec(fresh_src, "new-embed-model"),
            ]
        )
        await session.commit()

    bindings = {"embedding": {"model": "new-embed-model", "provider": "litellm"}}
    principal = Principal(user_id=owner, subject="agent", email="", actor_kind="aleph_agent")
    async with maker() as session:
        sources, chunks = await reembed_for_project(
            session,
            project_id=pid,
            client=_FakeEmbedClient(),
            principal=principal,
            profile_bindings=bindings,
            purpose="rks.reembed",
        )
        await session.commit()

    assert sources == 1  # only the stale source
    assert chunks == 1
    async with maker() as session:
        stale_chunk = (
            await session.execute(select(DocumentChunk).where(DocumentChunk.source_id == stale_src))
        ).scalar_one()
        assert stale_chunk.embedder_model == "new-embed-model"

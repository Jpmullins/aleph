"""Concurrent ingest must not collide on `Source.short_id`.

`_next_short_id` allocated ids with `COUNT(*) + 1` against a column carrying a
**global** unique constraint. That is a lost-update race:

    n = SELECT count(*) FROM sources   -- every concurrent caller reads 588
    return f"S{n + 1:04d}"             -- every one returns "S0589"

The first insert wins; the rest raise `UniqueViolationError`, which surfaces to
the agent as `sources/ingest-url failed (500)`. A real research run ingests
papers in parallel — eight arrived in the same second — so one succeeded and
seven failed, and the analyst saw "all the ingests are failing".

**A sequential test passes on the broken implementation.** Ingest one paper,
then another, and `COUNT(*) + 1` is correct every time. That is why this shipped
and why the test below deliberately runs the allocations *concurrently*: the
defect only exists between the read and the insert.

The second failure was quieter. Counting is not monotonic — delete or retract a
source and the count drops, so the next allocation reissues an id that committed
wiki prose already cites as `[[Source:S0042]]`, silently re-pointing those
citations at a different paper.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: Enough parallelism to lose the race reliably. The observed failure had eight
#: papers land in the same second.
CONCURRENCY = 8


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "ingest", "email": "i@test.local", "name": "I"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _project(http_client) -> UUID:
    resp = await http_client.post(
        "/v1/projects", json={"title": f"ingest {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return UUID(resp.json()["id"])


async def _register(asgi_app, project_id: UUID, i: int):
    """One real `register_uploaded_source`, in its own session."""
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_rks.source_service import register_uploaded_source
    from aleph_security.principal import Principal
    from tests.e2e.test_citation_provenance import _dev_user_id

    maker = asgi_app.state.session_maker
    principal = Principal(
        user_id=(await _dev_user_id(maker)),
        subject="dev@aleph.local",
        email="dev@aleph.local",
        actor_kind="user",
    )
    async with maker() as session:
        created = await register_uploaded_source(
            session,
            ledger=LedgerWriter(session),
            principal=principal,
            asset_store=asgi_app.state.asset_store,
            project_id=project_id,
            title=f"Paper {i}",
            data=f"# Paper {i}\n\nBody.\n".encode(),
            filename=f"paper-{i}.md",
            mime_type="text/markdown",
        )
        await session.commit()
        return created.source.short_id


class TestConcurrentIngest:
    async def test_parallel_ingests_all_succeed_with_distinct_ids(
        self, asgi_app, http_client, auth_bypass
    ):
        """The headline. Eight at once, eight distinct ids, no exceptions."""
        project_id = await _project(http_client)

        results = await asyncio.gather(
            *[_register(asgi_app, project_id, i) for i in range(CONCURRENCY)],
            return_exceptions=True,
        )

        failures = [r for r in results if isinstance(r, BaseException)]
        assert not failures, (
            f"{len(failures)} of {CONCURRENCY} concurrent ingests failed. This is "
            f"the COUNT(*)+1 race: every caller computed the same short_id and "
            f"all but one hit the unique constraint, surfacing as a 500 from "
            f"ingest-url. First failure: {failures[0]!r}"
        )
        short_ids = [r for r in results if isinstance(r, str)]
        assert len(set(short_ids)) == CONCURRENCY, (
            f"ids collided: {sorted(short_ids)} — a duplicate short_id makes "
            f"`[[Source:...]]` citation markers ambiguous"
        )

    async def test_ids_are_not_reused_after_a_source_is_deleted(
        self, asgi_app, http_client, auth_bypass
    ):
        """Counting was non-monotonic; a reissued id re-points live citations."""
        from aleph_rks.models import Source

        project_id = await _project(http_client)
        first = await _register(asgi_app, project_id, 0)

        async with asgi_app.state.session_maker() as session:
            row = (
                await session.execute(select(Source).where(Source.short_id == first))
            ).scalar_one()
            await session.delete(row)
            await session.commit()

        second = await _register(asgi_app, project_id, 1)
        assert second != first, (
            f"short_id {first} was reissued after the original was deleted. Any "
            f"committed wiki prose citing [[Source:{first}]] now resolves to a "
            f"different paper, with nothing to indicate it changed."
        )

    async def test_allocation_does_not_depend_on_row_count(
        self, asgi_app, http_client, auth_bypass
    ):
        """Guards the actual mechanism, not just its current output.

        A reimplementation that goes back to counting would still pass the two
        tests above on a quiet database. This one fails immediately.
        """
        from aleph_rks.models import Source

        project_id = await _project(http_client)
        async with asgi_app.state.session_maker() as session:
            before = (await session.execute(select(func.count()).select_from(Source))).scalar_one()

        short_id = await _register(asgi_app, project_id, 99)
        numeric = int(short_id[1:])
        assert numeric != before + 1 or numeric > before + 1, (
            "short_id tracks COUNT(*)+1 exactly, which is the racy allocation "
            "this test exists to prevent"
        )

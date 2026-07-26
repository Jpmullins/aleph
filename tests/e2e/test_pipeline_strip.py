"""Corpus progress must be countable, and a stalled corpus must look stalled.

A source's journey — fetch → normalize → chunk+embed → wiki — runs entirely in
workers. Nothing surfaced it, so "is my library ready to ask questions of?" was
unanswerable without reading container logs, and a run that stalled after
normalization looked exactly like one that finished.

Two ways a progress strip lies, and both are the house failure mode:

* **Non-cumulative counts.** Counting each status exclusively makes sources
  *leave* `normalized` as they advance to `indexed`. The number goes down, which
  reads as work being lost rather than work progressing.
* **Swallowed failures.** A source with `status="failed"` that is simply absent
  from every stage makes a broken corpus look like a smaller one. That is the
  corpus-level version of the join that silently returns nothing.

These tests drive the real route against real rows.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _project_with_sources(asgi_app, http_client, statuses: list[str]) -> str:
    """Create the project over the API (so membership exists), then seed sources.

    Creating the row directly leaves the caller a non-member and every read
    404s — the project-scope dependency is doing its job, so the test has to go
    through the same door a user does.
    """
    from uuid import UUID

    from aleph_rks.models import Source
    from tests.e2e.test_citation_provenance import _dev_user_id

    resp = await http_client.post(
        "/v1/projects", json={"title": f"pipeline {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]

    maker = asgi_app.state.session_maker
    user_id = await _dev_user_id(maker)
    async with maker() as session:
        for i, status in enumerate(statuses):
            session.add(
                Source(
                    id=uuid7(),
                    project_id=UUID(project_id),
                    short_id=f"S{uuid4().hex[:4].upper()}{i}",
                    title=f"src {i}",
                    url=f"https://example.invalid/{i}",
                    connector_kind="upload",
                    status=status,
                    source_metadata_jsonb={},
                    created_by=user_id,
                    access_scope="project",
                )
            )
        await session.commit()
    return project_id


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "pipeline", "email": "p@test.local", "name": "P"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _pipeline(http_client, project_id: str) -> dict:
    resp = await http_client.get(f"/v1/projects/{project_id}/pipeline")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _by_key(body: dict) -> dict[str, int]:
    return {s["key"]: s["count"] for s in body["stages"]}


class TestCumulativeCounts:
    async def test_advanced_sources_still_count_in_earlier_stages(
        self, asgi_app, http_client, auth_bypass
    ):
        """The headline. A source on the wiki has been through every stage.

        Exclusive counting would report ingested=0 here, which reads as the
        corpus having been emptied rather than fully processed.
        """
        pid = await _project_with_sources(asgi_app, http_client, ["wiki_done"])
        counts = _by_key(await _pipeline(http_client, pid))
        assert counts == {
            "ingested": 1,
            "normalized": 1,
            "indexed": 1,
            "wiki_done": 1,
        }, f"a fully-processed source did not count in earlier stages: {counts}"

    async def test_counts_are_monotonically_non_increasing(
        self, asgi_app, http_client, auth_bypass
    ):
        """Later stages can never exceed earlier ones; that would be nonsense."""
        pid = await _project_with_sources(
            asgi_app,
            http_client,
            ["ingested", "ingested", "normalized", "indexed", "wiki_done"],
        )
        body = await _pipeline(http_client, pid)
        counts = [s["count"] for s in body["stages"]]
        assert counts == sorted(counts, reverse=True), (
            f"stage counts increase along the pipeline: {counts} — a source "
            f"appears in a later stage without having passed an earlier one"
        )
        assert counts[0] == 5

    async def test_a_stalled_corpus_is_visibly_stalled(self, asgi_app, http_client, auth_bypass):
        """The question the strip exists to answer."""
        pid = await _project_with_sources(asgi_app, http_client, ["normalized"] * 3)
        counts = _by_key(await _pipeline(http_client, pid))
        assert counts["normalized"] == 3
        assert counts["indexed"] == 0, (
            "nothing has been chunked or embedded, so the corpus is not "
            "queryable — the strip must show that as a gap, not as progress"
        )


class TestFailuresAreVisible:
    async def test_failed_sources_are_reported_not_dropped(
        self, asgi_app, http_client, auth_bypass
    ):
        pid = await _project_with_sources(
            asgi_app, http_client, ["wiki_done", "failed", "wiki_failed"]
        )
        body = await _pipeline(http_client, pid)
        assert body["failed"] == 2, (
            "failed sources vanished from the report; a broken corpus would "
            "render as a smaller healthy one"
        )
        assert body["total"] == 3

    async def test_failures_are_not_folded_into_stage_counts(
        self, asgi_app, http_client, auth_bypass
    ):
        """A failure must not be able to masquerade as progress."""
        pid = await _project_with_sources(asgi_app, http_client, ["failed", "wiki_failed"])
        body = await _pipeline(http_client, pid)
        assert _by_key(body)["ingested"] == 0
        assert body["failed"] == 2


class TestScoping:
    async def test_empty_project_reports_zero_not_an_error(
        self, asgi_app, http_client, auth_bypass
    ):
        pid = await _project_with_sources(asgi_app, http_client, [])
        body = await _pipeline(http_client, pid)
        assert body["total"] == 0
        assert all(s["count"] == 0 for s in body["stages"])

    async def test_counts_do_not_leak_across_projects(self, asgi_app, http_client, auth_bypass):
        a = await _project_with_sources(asgi_app, http_client, ["wiki_done", "wiki_done"])
        b = await _project_with_sources(asgi_app, http_client, ["ingested"])
        assert (await _pipeline(http_client, a))["total"] == 2
        assert (await _pipeline(http_client, b))["total"] == 1


def test_strip_is_mounted_and_does_not_invent_its_own_stages() -> None:
    """The component must render the server's stages, not a hardcoded list.

    A client-side copy of the pipeline would drift from the worker states the
    moment one is added, and would drift silently.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    strip = (root / "apps/web/src/components/PipelineStrip.tsx").read_text()
    workspace = (root / "apps/web/src/components/ProjectWorkspace.tsx").read_text()

    assert "<PipelineStrip" in workspace, "the strip is not mounted anywhere"
    assert "data.stages.map" in strip, "the strip does not render server-sent stages"
    for invented in ('"ingested"', '"normalized"', '"indexed"', '"wiki_done"'):
        assert invented not in strip, (
            f"{invented} is hardcoded in the client; add a worker state and the "
            f"strip silently stops reflecting the pipeline"
        )

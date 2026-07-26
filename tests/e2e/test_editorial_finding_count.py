"""E1.4 — a review run's `finding_count` must equal the findings it wrote.

Each of the five editorial reviewers is registered through `_wrap(handler,
slot)`, which returns `{slot: n}` — `n_c`, `n_w`, `n_n`, `n_g`, `n_f`. None of
those slots were declared on `EditorialReviewState`, so LangGraph **discarded
every one**. `_node_finalize` then summed five absent keys via
`state.get("n_c") or 0` and persisted `finding_count=0`.

The findings themselves were written correctly, to the database, by
`_run_subagent`. So the run reported "0 findings" while its own findings sat in
`review_findings` — and anything reading the count (a queue badge, a
notification, a decision about whether review is needed) saw a clean review of
a page that had just failed five checks.

That is the failure mode this whole effort keeps meeting: the work happens, the
number that summarises it is zero, and nothing errors.

The gateway is faked at the boundary — a canned JSON response per reviewer, the
only thing between the graph and a model. Everything downstream is real: the
compiled graph, `_run_subagent`'s parsing and row writes, `finalize_run`'s
persistence, and the two tables the assertion compares.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: One finding per reviewer, five reviewers. `severity="low"` deliberately: a
#: medium+ finding also creates an ApprovalRequest, and this test is about the
#: count, not the approval pairing.
_ONE_FINDING = {
    "findings": [
        {
            "kind": "narrative_gap",
            "severity": "low",
            "title": "A gap",
            "description": "Something is missing.",
            "target_page_id": None,
            "evidence_refs": [],
        }
    ]
}


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "editorial", "email": "e@test.local", "name": "E"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


def _canned_gateway(monkeypatch, payload: dict) -> None:
    """The only fake: the HTTP boundary to the model."""
    from aleph_models import client as client_mod

    async def fake_post(self, path, body):
        if path != "/v1/chat/completions":
            msg = f"unexpected gateway path {path}"
            raise AssertionError(msg)
        return {
            "id": "chatcmpl-editorial",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(payload)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }

    monkeypatch.setattr(client_mod.LiteLLMClient, "_post_with_retry", fake_post)


async def _project(http_client) -> str:
    resp = await http_client.post(
        "/v1/projects", json={"title": f"editorial {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _run_editorial(asgi_app, project_id: str) -> int:
    """Drive the REAL compiled editorial graph."""
    from aleph_db.repos import model_profile as profile_repo
    from aleph_reviewer.editorial.workflow import EditorialReviewerWorkflow
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
        profile = await profile_repo.get_project_profile(session, UUID(project_id))
    assert profile is not None

    workflow = EditorialReviewerWorkflow(
        session_maker=maker,
        litellm=asgi_app.state.litellm,
        principal=principal,
        profile=profile,
    )
    return await workflow.run(project_id=UUID(project_id), agent_run_id=uuid7(), trigger="test")


async def _counts(asgi_app, project_id: str) -> tuple[int, int]:
    """`(persisted finding_count, actual ReviewFinding rows)`."""
    from aleph_reviewer.models import ReviewFinding, ReviewRun

    async with asgi_app.state.session_maker() as session:
        run = (
            (
                await session.execute(
                    select(ReviewRun)
                    .where(ReviewRun.project_id == UUID(project_id))
                    .order_by(ReviewRun.started_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert run is not None, "no ReviewRun row was written at all"
        rows = (
            await session.execute(
                select(func.count())
                .select_from(ReviewFinding)
                .where(ReviewFinding.review_run_id == run.id)
            )
        ).scalar_one()
        return int(run.finding_count), int(rows)


class TestFindingCountMatchesRows:
    async def test_persisted_count_equals_rows_written(
        self, asgi_app, http_client, auth_bypass, monkeypatch
    ):
        """The headline. Five reviewers, one finding each, count must be five."""
        _canned_gateway(monkeypatch, _ONE_FINDING)
        project_id = await _project(http_client)
        returned = await _run_editorial(asgi_app, project_id)
        persisted, rows = await _counts(asgi_app, project_id)

        assert rows == 5, f"expected one finding per reviewer, found {rows} rows"
        assert persisted == rows, (
            f"ReviewRun.finding_count={persisted} but {rows} ReviewFinding rows "
            f"exist. The per-reviewer counts were dropped by LangGraph, so the "
            f"run reports a clean review of a page that just failed five checks."
        )
        assert returned == rows, (
            f"run() returned {returned}; the caller (worker, badge, notification) "
            f"sees a different number from the database"
        )

    async def test_zero_findings_is_a_real_zero(
        self, asgi_app, http_client, auth_bypass, monkeypatch
    ):
        """A genuine clean review must be distinguishable from a dropped count.

        Without this the headline test could be satisfied by any non-zero
        number, and `finding_count == 0` would stay ambiguous.
        """
        _canned_gateway(monkeypatch, {"findings": []})
        project_id = await _project(http_client)
        await _run_editorial(asgi_app, project_id)
        persisted, rows = await _counts(asgi_app, project_id)
        assert (persisted, rows) == (0, 0)

    async def test_count_scales_with_findings_per_reviewer(
        self, asgi_app, http_client, auth_bypass, monkeypatch
    ):
        """Two findings per reviewer → ten. Pins the sum, not just non-zero."""
        two = {"findings": _ONE_FINDING["findings"] * 2}
        _canned_gateway(monkeypatch, two)
        project_id = await _project(http_client)
        await _run_editorial(asgi_app, project_id)
        persisted, rows = await _counts(asgi_app, project_id)
        assert rows == 10, f"expected 10 findings, found {rows}"
        assert persisted == 10, f"finding_count={persisted}, rows={rows}"

    async def test_run_is_marked_completed(self, asgi_app, http_client, auth_bypass, monkeypatch):
        """`finalize_run` writes the count and the status in the same call."""
        from aleph_reviewer.models import ReviewRun

        _canned_gateway(monkeypatch, _ONE_FINDING)
        project_id = await _project(http_client)
        await _run_editorial(asgi_app, project_id)
        async with asgi_app.state.session_maker() as session:
            run = (
                (
                    await session.execute(
                        select(ReviewRun).where(ReviewRun.project_id == UUID(project_id))
                    )
                )
                .scalars()
                .first()
            )
        assert run is not None
        assert run.status == "completed"
        assert run.completed_at is not None

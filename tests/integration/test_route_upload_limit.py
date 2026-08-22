"""A body over the upload limit is refused at the wire, with 413. WS-P3 c3.

## What was already pinned, and what was not

`apps/api/tests/unit/test_upload_is_bounded.py` drives `_read_bounded` directly
with a stub `UploadFile` and proves the *read pattern*: the refusal costs a
bounded number of chunk reads rather than buffering the whole body first. That
is the half a wire test cannot see, and it stays.

What nothing checked was the route. `grep -rn '413' tests/ apps/api/tests`
returned three hits, all in that unit file. No request had ever been made to
`POST /v1/projects/{id}/sources/upload` with a body over the limit, so the
criterion's actual claim — *the API answers 413* — rested on reading the code.
Three things could break it with the unit test still green: the route could
stop calling the helper, FastAPI could translate the `HTTPException` into
something else, or a middleware could turn it into a 500 on the way out.

## The four assertions, and why each is load-bearing

* **413, not 500.** The criterion.
* **413, not 201.** A route that read only the first N bytes and stored a
  truncated file would also "not 500". The refusal has to be a refusal.
* **Nothing was written.** The over-limit POST leaves no `sources` row, so the
  bytes were rejected rather than accepted-and-then-complained-about.
* **A normal upload still succeeds.** Without this, a route that answered 413
  to *everything* — `aleph_max_upload_bytes` misread as 0, say — would pass
  every assertion above. This is the one that makes the others mean something.

## How the limit is set

`upload_source` reads `request.app.state.settings.aleph_max_upload_bytes` on
every call, so the fixture lowers it on the booted app rather than posting 64
MiB through an in-process transport. That is the production configuration knob,
read from the production place; the size in the refusal message is derived from
it, which is why the message assertion cannot pass against a hardcoded string.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, NamedTuple

import httpx
import pytest
from sqlalchemy import text

if TYPE_CHECKING:
    from fastapi import FastAPI

pytestmark = pytest.mark.integration

#: The limit the app is reconfigured to for this test. Whole MiB because the
#: refusal message reports `limit // (1024 * 1024)`, and a limit under 1 MiB
#: would render as "0 MiB" — true, and useless to whoever reads it.
TEST_LIMIT_BYTES = 2 * 1024 * 1024

#: A body one byte past the line. `_read_bounded` refuses on `> limit`, so this
#: is the smallest body that must be refused.
OVER = TEST_LIMIT_BYTES + 1

#: An ordinary upload. Deliberately tiny: the control only has to show the
#: route accepts something.
UNDER = b"# a small markdown source\n\nAleph upload limit control case.\n"

#: Teardown, in FK order. Same list and same reasoning as
#: `tests/integration/conftest.py`: `action_ledger_events` is append-only and is
#: NOT deleted, because a fixture that switches an invariant off to tidy up is
#: how the invariant stops being one.
_TEARDOWN = (
    "DELETE FROM document_chunks WHERE project_id = :pid",
    "DELETE FROM retrieval_index_records WHERE project_id = :pid",
    "DELETE FROM normalized_documents WHERE project_id = :pid",
    "DELETE FROM source_versions WHERE source_id IN"
    " (SELECT id FROM sources WHERE project_id = :pid)",
    "DELETE FROM source_assets WHERE project_id = :pid",
    "DELETE FROM sources WHERE project_id = :pid",
    "DELETE FROM agent_events WHERE agent_run_id IN"
    " (SELECT id FROM agent_runs WHERE project_id = :pid)",
    "DELETE FROM agent_runs WHERE project_id = :pid",
    "DELETE FROM model_profiles WHERE project_id = :pid",
    "DELETE FROM project_members WHERE project_id = :pid",
    "DELETE FROM projects WHERE id = :pid",
)


class Attempt(NamedTuple):
    """One POST to the upload route."""

    status: int
    body: str


class Run(NamedTuple):
    """Everything the booted app was asked, in one boot."""

    limit: int
    accepted: Attempt
    refused: Attempt
    #: `sources` rows for the throwaway project after both POSTs.
    sources_after: int


async def _upload(client: httpx.AsyncClient, project_id: str, name: str, data: bytes) -> Attempt:
    response = await client.post(
        f"/v1/projects/{project_id}/sources/upload",
        files={"file": (name, data, "text/markdown")},
    )
    return Attempt(response.status_code, response.text[:400])


async def _exercise() -> Run:
    """Boot the real app, create a project over HTTP, upload twice, clean up."""
    from aleph_api.main import create_app

    app: FastAPI = create_app()
    async with app.router.lifespan_context(app):
        settings = app.state.settings
        # Creating a project dispatches `bootstrap_project_job`, a full research
        # run. Same reasoning as `test_route_smoke.py`.
        previously_bootstrap = settings.bootstrap_auto_enabled
        previously_limit = settings.aleph_max_upload_bytes
        settings.bootstrap_auto_enabled = False
        settings.aleph_max_upload_bytes = TEST_LIMIT_BYTES

        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
        project_id = ""
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://upload-limit", timeout=120.0
            ) as client:
                created = await client.post(
                    "/v1/projects",
                    json={
                        "title": "[upload-limit] throwaway",
                        "description": "created by tests/integration/test_route_upload_limit.py",
                    },
                )
                assert created.status_code == 201, created.text
                project_id = str(created.json()["id"])

                accepted = await _upload(client, project_id, "control.md", UNDER)
                refused = await _upload(client, project_id, "too-big.md", b"\x41" * OVER)

            async with app.state.session_maker() as session:
                count = await session.scalar(
                    text("SELECT count(*) FROM sources WHERE project_id = :pid"),
                    {"pid": uuid.UUID(project_id)},
                )
            sources_after = int(count or 0)
        finally:
            settings.bootstrap_auto_enabled = previously_bootstrap
            settings.aleph_max_upload_bytes = previously_limit
            if project_id:
                async with app.state.session_maker() as session:
                    for statement in _TEARDOWN:
                        await session.execute(text(statement), {"pid": uuid.UUID(project_id)})
                    await session.commit()

    return Run(TEST_LIMIT_BYTES, accepted, refused, sources_after)


@pytest.fixture(scope="module")
def run() -> Run:
    """One boot, two uploads, shared by every assertion below."""
    return asyncio.run(_exercise())


def test_a_body_over_the_limit_is_refused_with_413(run: Run) -> None:
    """THE criterion, over the wire: 413, not a 500 and not a truncated 201."""
    assert run.refused.status == 413, run.refused.body


def test_the_refusal_names_the_setting_that_caused_it(run: Run) -> None:
    """A limit an operator cannot find is a limit they cannot change.

    The MiB figure is derived from the configured limit, so this cannot be
    satisfied by a hardcoded sentence that has drifted from the setting.
    """
    assert "ALEPH_MAX_UPLOAD_BYTES" in run.refused.body
    assert f"{run.limit // (1024 * 1024)} MiB" in run.refused.body


def test_the_refused_upload_stored_nothing(run: Run) -> None:
    """A truncated read that stored the prefix would answer 2xx — but so would
    a route that stored the prefix and *then* complained. Only the row count
    separates "refused" from "accepted and regretted"."""
    assert run.sources_after == 1, (
        f"{run.sources_after} sources for the throwaway project; expected exactly the "
        "one the control upload created"
    )


def test_an_ordinary_upload_is_still_accepted(run: Run) -> None:
    """The control. Without it, `aleph_max_upload_bytes` read as 0 passes everything above."""
    assert run.accepted.status == 201, run.accepted.body

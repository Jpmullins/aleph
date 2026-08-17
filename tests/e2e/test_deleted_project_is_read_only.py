"""A soft-deleted project must not accept writes — and must stay restorable.

Observed in production, from the action ledger:

    14:58:42  project.create   {"title": "[e2e] Context bar"}
    15:09:16  project.update   {"status": "deleted"}
    17:22        ← a research run ingested 20 papers into it,
                   computed embeddings, and built 223 wiki pages

Every step reported success. The project *list* filters on status; the write
paths ignored it entirely, so the work landed in a project nothing surfaces and
became unreachable. That is the house failure mode: the operation succeeds, the
state it writes is orphaned, and nothing errors.

The fix has one dangerous edge, and it is the reason most of this file exists.
`PATCH /v1/projects/{id}` with `{"status": "active"}` is the **only** way back
from `deleted`. A blanket "no writes to deleted projects" rule would refuse that
too — making the state permanent, the data unrecoverable, and the fix worse than
the bug. So the tests below spend more effort proving you can still get *out*
than proving you are kept out.

Reads stay open deliberately: a member has to be able to look at a project
before deciding to restore it, and the list already hides it.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "status", "email": "s@test.local", "name": "S"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _project(http_client, status: str | None = None) -> str:
    resp = await http_client.post(
        "/v1/projects", json={"title": f"status {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    if status:
        r = await http_client.patch(f"/v1/projects/{pid}", json={"status": status})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == status
    return pid


async def _try_write(http_client, pid: str):
    """A representative sub-resource write: uploading a source."""
    return await http_client.post(
        f"/v1/projects/{pid}/sources/ingest-url",
        json={"url": "https://example.invalid/p.pdf", "title": "P", "connector_kind": "upload"},
    )


class TestWritesAreRefused:
    @pytest.mark.parametrize("status", ["deleted", "archived"])
    async def test_ingest_into_a_non_active_project_is_refused(
        self, http_client, auth_bypass, status
    ):
        """The exact operation that silently succeeded for two hours."""
        pid = await _project(http_client, status)
        resp = await _try_write(http_client, pid)
        assert resp.status_code == 409, (
            f"ingest into a {status} project returned {resp.status_code}; it must "
            f"be refused. Accepting it is how 20 papers and 223 wiki pages were "
            f"written into a project nothing surfaces."
        )
        body = resp.json()
        assert status in body["detail"], f"the error does not say why: {body}"
        assert "restore" in body["detail"].lower(), (
            f"the error refuses without saying how to proceed: {body['detail']}"
        )

    @pytest.mark.parametrize("status", ["deleted", "archived"])
    async def test_sub_resource_patch_is_not_exempt(self, http_client, auth_bypass, status):
        """The restore exemption must be narrow.

        It keys on the project's own path. A PATCH to a *sub-resource* shares
        the method and the prefix, so a sloppy check would wave it through.
        """
        pid = await _project(http_client, status)
        resp = await http_client.patch(
            f"/v1/projects/{pid}/model-profile",
            json={"bindings": {"classification": {"model": "x", "provider": "litellm"}}},
        )
        assert resp.status_code == 409, (
            f"PATCH on a sub-resource of a {status} project returned "
            f"{resp.status_code}; the restore exemption is too broad"
        )

    async def test_active_projects_are_unaffected(self, http_client, auth_bypass):
        """The rule must not cost anything in the normal case."""
        pid = await _project(http_client)
        resp = await _try_write(http_client, pid)
        assert resp.status_code != 409, (
            f"an ACTIVE project refused a write with 409 — the status check is "
            f"firing when it should not. Body: {resp.text[:300]}"
        )


class TestRestoreIsAlwaysPossible:
    """The anti-lockout guarantee. If these fail, the fix is worse than the bug."""

    @pytest.mark.parametrize("status", ["deleted", "archived"])
    async def test_a_non_active_project_can_be_restored(self, http_client, auth_bypass, status):
        pid = await _project(http_client, status)
        resp = await http_client.patch(f"/v1/projects/{pid}", json={"status": "active"})
        assert resp.status_code == 200, (
            f"a {status} project could not be restored ({resp.status_code}): "
            f"{resp.text[:300]}. The write rule has locked the data away "
            f"permanently, which is worse than the defect it fixes."
        )
        assert resp.json()["status"] == "active"

    @pytest.mark.parametrize("status", ["deleted", "archived"])
    async def test_writes_work_again_after_restoring(self, http_client, auth_bypass, status):
        """Restoring must actually restore, not just flip a flag."""
        pid = await _project(http_client, status)
        await http_client.patch(f"/v1/projects/{pid}", json={"status": "active"})
        resp = await _try_write(http_client, pid)
        assert resp.status_code != 409, (
            f"still refusing writes after restore from {status}: {resp.text[:200]}"
        )

    async def test_a_restored_project_reappears_in_the_list(self, http_client, auth_bypass):
        """The symptom that made this unreachable: the list hides non-active."""
        pid = await _project(http_client, "deleted")
        listed = {p["id"] for p in (await http_client.get("/v1/projects")).json()}
        assert pid not in listed, "a deleted project is still listed"
        await http_client.patch(f"/v1/projects/{pid}", json={"status": "active"})
        listed = {p["id"] for p in (await http_client.get("/v1/projects")).json()}
        assert pid in listed, "a restored project did not come back to the list"


class TestReadsStayOpen:
    """You must be able to see a project in order to decide to restore it."""

    @pytest.mark.parametrize("status", ["deleted", "archived"])
    async def test_project_detail_is_readable(self, http_client, auth_bypass, status):
        pid = await _project(http_client, status)
        resp = await http_client.get(f"/v1/projects/{pid}")
        assert resp.status_code == 200, (
            f"a {status} project is unreadable ({resp.status_code}); a member "
            f"cannot inspect it before restoring, and the id came from somewhere"
        )
        assert resp.json()["status"] == status

    @pytest.mark.parametrize("status", ["deleted", "archived"])
    async def test_sub_resource_reads_still_work(self, http_client, auth_bypass, status):
        """Its content must remain visible — that is what makes restore informed."""
        pid = await _project(http_client, status)
        for path in (f"/v1/projects/{pid}/sources", f"/v1/projects/{pid}/pipeline"):
            resp = await http_client.get(path)
            assert resp.status_code == 200, (
                f"GET {path} on a {status} project returned {resp.status_code}"
            )


async def test_non_members_still_get_404_not_409(asgi_app, http_client, auth_bypass):
    """Status must not leak existence.

    409 says "this project exists and is deleted". For a project the caller is
    not a member of, that is a disclosure — 404 has to win, and it has to win
    *before* the status is consulted.
    """
    from aleph_db.models.model_profile import ModelProfile
    from aleph_db.repos import project as project_repo
    from tests.e2e.test_citation_provenance import _dev_user_id

    maker = asgi_app.state.session_maker
    user_id = await _dev_user_id(maker)
    async with maker() as session:
        profile = ModelProfile(
            id=__import__("aleph_core.ids", fromlist=["uuid7"]).uuid7(),
            project_id=None,
            name=f"nm-{uuid4().hex[:8]}",
            is_template=False,
            bindings_jsonb={},
            created_by=user_id,
            access_scope="project",
        )
        session.add(profile)
        await session.flush()
        # Created directly, so the caller is deliberately NOT a member.
        other = project_repo.new_project(
            title="not mine",
            description="",
            model_profile_id=profile.id,
            created_by=user_id,
        )
        other.status = "deleted"
        session.add(other)
        await session.commit()
        other_id = str(other.id)

    resp = await _try_write(http_client, other_id)
    assert resp.status_code == 404, (
        f"got {resp.status_code} for a deleted project the caller is not a member "
        f"of; 409 would confirm it exists and reveal its state"
    )
    assert UUID(other_id)


class TestTheRestorePathIsDiscoverable:
    """Refusing writes is only half a fix; being able to find the project is the other half.

    The 409 says "restore it first". That instruction is useless if the project
    cannot be found — and `list_member_projects` filters deleted rows, so before
    `include_deleted` the only way back was to already know the UUID. That is
    precisely what happened: a real corpus (20 sources, 223 wiki pages) became
    unreachable because the only door was hidden.
    """

    async def test_deleted_projects_are_listable_on_request(self, http_client, auth_bypass):
        pid = await _project(http_client, "deleted")

        default = {p["id"] for p in (await http_client.get("/v1/projects")).json()}
        assert pid not in default, "deleted projects must stay hidden by default"

        resp = await http_client.get("/v1/projects?include_deleted=true")
        assert resp.status_code == 200, resp.text
        shown = {p["id"]: p for p in resp.json()}
        assert pid in shown, (
            "a deleted project cannot be listed even on request, so the 409's "
            "advice to restore it is unactionable for anyone without the UUID"
        )
        assert shown[pid]["status"] == "deleted", "the UI needs the status to offer Restore"

    def test_the_ui_offers_restore_and_a_way_to_see_deleted(self) -> None:
        """The API affordance is worthless if nothing surfaces it."""
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[2] / "apps/web/src/components/ProjectList.tsx"
        ).read_text()
        assert "include_deleted=true" in src, "the list never asks for deleted projects"
        assert "show-deleted-projects" in src, "no control to reveal deleted projects"
        assert 'status: "active"' in src, "no restore mutation"
        assert "project-restore-" in src, "no Restore button"
        assert "reversible by an admin" not in src, (
            "the delete confirmation still claims only an admin can undo it, "
            "which was untrue and is now actively misleading"
        )

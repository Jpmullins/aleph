"""The kernel, reachable over HTTP. WS-A2.

CLAUDE.md's first substantive line is that Aleph is an agent that authors
plugins for itself on a kernel with guardrails, and that "the kernel is the
product". The kernel was built, guarded and covered by 153 tests, and
`grep -rn "AgentPluginAPI" apps/api/src` returned **0** — no route, no tool, no
graph node. The product was a library with one non-test importer, and that
importer was an acceptance probe.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Annotated, Any

import httpx
import pytest
from fastapi import Depends, Path
from sqlalchemy import select

from aleph_api.deps import principal_dep
from aleph_api.middleware.project_scope import project_scope_dep
from aleph_kernel.agent_api import AgentPluginAPI
from aleph_kernel.kernel import Kernel
from aleph_kernel.spec import CapabilitySpec
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole

pytestmark = pytest.mark.integration


def _spec(name: str, *, provides: tuple[str, ...], requires: tuple[str, ...] = ()) -> Any:
    """A minimal working capability.

    `setup` is an async GENERATOR, not a coroutine — the kernel drives it as an
    async iterator so a capability can yield its own inverse. A plain `async
    def` here fails at install with
    `TypeError: 'coroutine' object is not an async iterator`, which is what an
    earlier version of this fixture did.
    """

    async def _setup(ctx: Any) -> Any:
        for key in provides:
            ctx.provide(key, f"{name}:{key}")
        if False:  # pragma: no cover - never taken; makes this a generator
            yield

    async def _probe(_ctx: Any) -> Any:
        from aleph_kernel.spec import ok

        return ok(f"{name} responded")

    return CapabilitySpec(
        name=name,
        provides=frozenset(provides),
        requires=frozenset(requires),
        setup=_setup,
        probe=_probe,
    )


async def test_preview_matches_the_refusal() -> None:
    """THE criterion, and the reason preview exists at all.

    A refusal an operator could not have predicted is indistinguishable from a
    broken button. Preview and refusal read the SAME declaration graph, so this
    asserts they name the same set rather than that each is individually
    plausible.
    """
    kernel = Kernel()
    api = AgentPluginAPI(kernel)

    a = await api.install(_spec("cap-a", provides=("thing.a",)))
    b = await api.install(_spec("cap-b", provides=("thing.b",), requires=("thing.a",)))
    assert a.installed and b.installed

    previewed = {
        tuple(sorted(getattr(v, "would_also_stop", ()) or ()))
        for v in api.inspect()
        if v.name == "cap-a"
    }
    assert previewed == {("cap-b",)}, previewed

    from aleph_kernel.kernel import PluginId

    outcome = await api.disable(PluginId(uuid.UUID(str(a.plugin_id))))
    # `installed=True` is the REFUSAL: the plugin is still installed. The field
    # answers "is it installed", not "did the call succeed", and reading it the
    # intuitive way inverts the check — which is exactly what the first version
    # of this test, the route and the agent tool all did.
    assert outcome.installed, "the disable was allowed despite a dependent"
    assert "refused" in outcome.detail
    assert "cap-b" in outcome.detail, (
        f"preview said cap-b and the refusal said {outcome.detail!r} — an "
        "operator cannot trust a preview that disagrees with the thing it "
        "previews"
    )


async def test_core_capability_has_no_handle_to_pass_anywhere() -> None:
    """Not "refused" — UNEXPRESSIBLE.

    A capability mounted from the boot manifest has `plugin_id = None`, so
    there is no id to put in a URL or hand to a tool. The route surface carries
    ids rather than names precisely so it inherits that.
    """
    kernel = Kernel()
    kernel.register_core(_spec("core-thing", provides=("core.thing",)))
    api = AgentPluginAPI(kernel)

    core = [v for v in api.inspect() if v.name == "core-thing"]
    assert core, "the core capability vanished from inspect()"
    assert core[0].plugin_id is None
    assert core[0].removable is False


async def test_the_route_module_reaches_the_kernel_through_the_agent_api() -> None:
    """Criterion 1, as a property of the code rather than a grep of it.

    `grep -rn AgentPluginAPI apps/api/src | wc -l >= 1` is satisfied by a
    docstring. This asserts the route module actually constructs one.
    """
    import ast
    import pathlib

    src = pathlib.Path("apps/api/src/aleph_api/routes/plugins.py")
    tree = ast.parse(src.read_text())  # noqa: ASYNC240 - a source read, not I/O in a request
    constructed = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "AgentPluginAPI"
    ]
    assert constructed, "the route module names AgentPluginAPI but never builds one"


def test_the_agent_has_the_five_kernel_tools() -> None:
    """And every one is classified for the interpreter loop.

    `test_every_orchestrator_tool_is_classified` enforces the partition; this
    asserts the tools exist to be partitioned, and that the two that CHANGE what
    the system can do are the two withheld from a loop.
    """
    from aleph_api.copilot_agent import _ORCHESTRATOR_TOOLS
    from aleph_api.interpreter import PTC_ALLOWLIST, PTC_WITHHELD

    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in _ORCHESTRATOR_TOOLS}
    assert {
        "list_capabilities",
        "preview_removal",
        "author_plugin",
        "disable_plugin",
        "plugin_health",
    } <= names

    # Reads may loop; the two that alter the system's own abilities may not.
    assert "preview_removal" in PTC_ALLOWLIST
    assert "list_capabilities" in PTC_ALLOWLIST
    assert "author_plugin" in PTC_WITHHELD
    assert "disable_plugin" in PTC_WITHHELD


def test_authoring_requires_owner_not_merely_project_access() -> None:
    """A plugin is code this process executes and an instruction the model
    follows. That is not an editing action."""
    import ast
    import pathlib

    src = pathlib.Path("apps/api/src/aleph_api/routes/plugins.py").read_text()
    tree = ast.parse(src)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "author_plugin"
    )
    calls = [
        ast.unparse(n)
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "require_at_least"
    ]
    assert calls, "author_plugin does not check a role at all"
    assert any("ProjectRole.OWNER" in c for c in calls), calls


# ---------------------------------------------------------------------------
# The routes, over HTTP. WS-A2 criteria 4, 5 and 7; WS-A4 criterion 6.
# ---------------------------------------------------------------------------
#
# Every test above this line is kernel- or AST-level: they build a bare
# `Kernel()` in memory, or parse this module's source and assert a call node is
# present. Not one of them issues a request, so the four properties the criteria
# are actually about — what `GET /plugins` reports for core capability, what a
# fabricated id gets, what a non-owner gets, and whether a disable is ledgered —
# were asserted nowhere. An AST test proving `require_at_least(...OWNER)` appears
# in the function body cannot tell you the role gate RUNS; a role gate no request
# can fail is an assumption.
#
# Finding: it did not run correctly. Driving these over HTTP turned up two live
# defects in `routes/plugins.py`, both of them the same prefix mismatch between
# the kernel's `skill.<name>` and the `plugins` table's `<name>`:
#
#   * `POST /plugins` compared `view.name` to `row.name`, never matched, and so
#     ALWAYS returned `plugin_id: null` for a plugin that had mounted fine —
#     which is why nobody noticed the second one, since the caller was never
#     handed a handle to DELETE with.
#   * `DELETE /plugins/{id}` passed the capability name to
#     `PluginService.disable`, which selects on `Plugin.name`. No row matched,
#     the row stayed `installed`, and the `plugin.disable` ledger event was
#     never written — while the route returned `{"disabled": true}`, because the
#     kernel half really had succeeded.


def _principal(user_id: uuid.UUID) -> Principal:
    return Principal(
        user_id=user_id, subject="local-dev", email="dev@example.com", actor_kind="user"
    )


def _app(
    monkeypatch: pytest.MonkeyPatch,
    maker: Any,
    principal: Principal,
    *,
    kernel: Any,
    role: ProjectRole = ProjectRole.OWNER,
) -> Any:
    """`create_app`, with the caller's role and the kernel both explicit.

    The role is a parameter because every other plugin test caches OWNER, and a
    gate nothing can fail is not a gate. The kernel is a parameter because
    `lifespan` is what normally mounts it and `ASGITransport` does not run one.

    `Annotated`, `Path`, `Depends` and `principal_dep` are imported at MODULE
    level, not here. `from __future__ import annotations` turns `_scope`'s
    annotations into strings that FastAPI resolves against this module's
    globals, so a local import leaves them undefined and every request dies in
    `TypeAdapter` with "is not fully defined".
    """
    from aleph_api.main import create_app
    from aleph_api.middleware import auth as auth_mw

    app = create_app()
    app.state.settings = SimpleNamespace(aleph_auth_mode="local")
    app.state.session_maker = maker
    app.state.kernel = kernel

    async def _fake_local_dev(_request: Any) -> Any:
        return principal

    monkeypatch.setattr(auth_mw, "_principal_local_dev", _fake_local_dev)

    async def _scope(
        project_id: Annotated[uuid.UUID, Path(...)],
        p: Annotated[Principal, Depends(principal_dep)],
    ) -> uuid.UUID:
        p.cache_role(project_id, role.value)
        return project_id

    app.dependency_overrides[project_scope_dep] = _scope
    return app


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _boot_manifest_kernel() -> Any:
    """A kernel carrying the REAL core capability set, registered not activated.

    `mount_manifest` is what `lifespan` calls, and it only registers — `boot()`
    activates. That split is what lets this test assert the addressability
    property against the capabilities Aleph actually ships without needing
    Postgres, Redis, an object store and a gateway to be reachable first.
    """
    from aleph_api.lifespan import BOOT_MANIFEST
    from aleph_api.settings import get_settings
    from aleph_kernel.manifest import load_manifest, mount_manifest

    kernel = Kernel()
    mount_manifest(kernel, load_manifest(BOOT_MANIFEST), settings=get_settings())
    return kernel


def _manifest_names() -> set[str]:
    """Every capability the boot manifest declares, read from the file itself.

    Read from `aleph.toml` rather than from the kernel the test just built, so
    the assertion cannot be satisfied by a kernel that mounted nothing.
    """
    import tomllib

    from aleph_api.lifespan import BOOT_MANIFEST

    raw = tomllib.loads(BOOT_MANIFEST.read_text(encoding="utf-8"))
    return {row["name"] for row in raw["capability"]}


_SKILL = """\
---
name: http-probe-skill
description: A skill installed over HTTP by a test.
---

Do the thing, then check the thing.
"""


async def test_no_core_capability_is_addressable_over_http(
    monkeypatch: pytest.MonkeyPatch, maker: Any, committed_project: uuid.UUID
) -> None:
    """WS-A2 c4. Every capability Aleph boots with, over the real route.

    The existing witness builds one fabricated `CapabilitySpec` and asserts
    `inspect()[0].plugin_id is None`. That proves the kernel's rule; it does not
    prove the ROUTE inherits it, and it says nothing about the ten capabilities
    this deployment actually mounts.
    """
    app = _app(monkeypatch, maker, _principal(uuid.uuid4()), kernel=_boot_manifest_kernel())
    async with _client(app) as client:
        response = await client.get(f"/v1/projects/{committed_project}/plugins")

    assert response.status_code == 200, response.text
    body = response.json()
    reported = {row["name"] for row in body}
    declared = _manifest_names()
    assert declared, "the boot manifest declares nothing; this test would pass vacuously"
    assert declared <= reported, f"missing from the route: {sorted(declared - reported)}"

    for row in body:
        if row["name"] not in declared:
            continue
        assert row["plugin_id"] is None, row
        assert row["removable"] is False, row
        assert row["trust"] == "core", row


async def test_a_fabricated_plugin_id_is_unaddressable_not_merely_refused(
    monkeypatch: pytest.MonkeyPatch, maker: Any, committed_project: uuid.UUID
) -> None:
    """WS-A2 c4, the other half: there is no id to guess.

    404 rather than 403, and the body says why — "mounted from the boot
    manifest and has no plugin id". A 403 would tell an agent the id names
    something real and that a different one might work.
    """
    app = _app(monkeypatch, maker, _principal(uuid.uuid4()), kernel=_boot_manifest_kernel())
    fabricated = uuid.uuid4()
    async with _client(app) as client:
        deleted = await client.delete(f"/v1/projects/{committed_project}/plugins/{fabricated}")
        previewed = await client.get(
            f"/v1/projects/{committed_project}/plugins/{fabricated}/removal-preview"
        )

    assert deleted.status_code == 404, deleted.text
    assert "boot manifest" in deleted.json()["detail"]
    assert previewed.status_code == 404, previewed.text
    assert "boot manifest" in previewed.json()["detail"]


async def test_authoring_over_http_is_refused_for_a_member_who_is_not_owner(
    monkeypatch: pytest.MonkeyPatch, maker: Any, committed_project: uuid.UUID
) -> None:
    """WS-A2 c5, as a response rather than as a parsed source file.

    `test_authoring_requires_owner_not_merely_project_access` reads the module
    with `ast` and asserts `require_at_least(..., ProjectRole.OWNER)` is present.
    That is satisfied by a call whose result is discarded, by one guarded behind
    a condition that is never true, and by one on a route no request reaches.
    This holds an EDITOR principal — a real member of the project, in scope, who
    passes `project_scope_dep` — and asserts 403.
    """
    body = {"name": "http-probe-skill", "instructions": _SKILL, "code": ""}
    editor = _app(
        monkeypatch,
        maker,
        _principal(uuid.uuid4()),
        kernel=Kernel(),
        role=ProjectRole.EDITOR,
    )
    async with _client(editor) as client:
        refused = await client.post(f"/v1/projects/{committed_project}/plugins", json=body)
    assert refused.status_code == 403, refused.text

    # And the gate is not "refuse everybody". A VIEWER may still list.
    viewer = _app(
        monkeypatch,
        maker,
        _principal(uuid.uuid4()),
        kernel=_boot_manifest_kernel(),
        role=ProjectRole.VIEWER,
    )
    async with _client(viewer) as client:
        listed = await client.get(f"/v1/projects/{committed_project}/plugins")
    assert listed.status_code == 200, listed.text


async def test_disable_over_http_writes_a_ledger_row(
    monkeypatch: pytest.MonkeyPatch, maker: Any, committed_project: uuid.UUID
) -> None:
    """WS-A2 c7. Install over HTTP, disable over HTTP, read the ledger.

    `plugin.install` was pinned; `plugin.disable` — its sibling, defined three
    lines away in the same module and appended by the same service — was not.
    That asymmetry is what let `DELETE` return `{"disabled": true}` while writing
    neither the row edit nor the event.
    """
    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_db.models.plugin import Plugin
    from aleph_runtime.plugin_service import PLUGIN_DISABLED

    actor = uuid.uuid4()
    app = _app(monkeypatch, maker, _principal(actor), kernel=Kernel())
    async with _client(app) as client:
        created = await client.post(
            f"/v1/projects/{committed_project}/plugins",
            json={"name": "http-probe-skill", "instructions": _SKILL, "code": ""},
        )
        assert created.status_code == 201, created.text
        plugin_id = created.json()["plugin_id"]
        # The handle is the whole point. `null` here means the caller cannot
        # reach DELETE at all, which is the state this route shipped in.
        assert plugin_id is not None, created.json()
        assert created.json()["trust"] == "authored", created.json()

        deleted = await client.delete(f"/v1/projects/{committed_project}/plugins/{plugin_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["disabled"] is True

    async with maker() as session:
        events = list(
            (
                await session.execute(
                    select(ActionLedgerEvent).where(
                        ActionLedgerEvent.project_id == committed_project,
                        ActionLedgerEvent.action_kind == PLUGIN_DISABLED,
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = list(
            (await session.execute(select(Plugin).where(Plugin.project_id == committed_project)))
            .scalars()
            .all()
        )

    assert len(events) == 1, f"{PLUGIN_DISABLED} rows: {len(events)}"
    assert events[0].payload_jsonb["name"] == "http-probe-skill", events[0].payload_jsonb
    assert events[0].actor_id == actor
    # The ledger event and the row have to agree, or the event records something
    # that did not happen.
    assert [r.state for r in rows] == ["disabled"], [(r.name, r.state) for r in rows]
    assert events[0].target_id == rows[0].id


async def test_an_authored_plugins_trust_is_visible_over_http(
    monkeypatch: pytest.MonkeyPatch, maker: Any, committed_project: uuid.UUID
) -> None:
    """WS-A4 c6, the observable half.

    `trust` is DERIVED, not asserted by the plugin: a contribution that declares
    itself `core` still reports `authored`, because the only capabilities that
    report `core` are the ones with no `plugin_id`, and a `plugin_id` is minted
    only by `register_dynamic`. The behavioural half of the criterion —
    `requires_approval: true` on an authored save — was deliberately not built;
    `aleph_runtime.ui_contributions` carries the reasoning.
    """
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS, UIContribution

    app = _app(monkeypatch, maker, _principal(uuid.uuid4()), kernel=Kernel())
    async with _client(app) as client:
        created = await client.post(
            f"/v1/projects/{committed_project}/plugins",
            json={"name": "http-probe-skill", "instructions": _SKILL, "code": ""},
        )
        assert created.status_code == 201, created.text
        assert created.json()["trust"] == "authored"

        UI_CONTRIBUTIONS.remove("http-probe-skill")
        UI_CONTRIBUTIONS.register(
            UIContribution(
                plugin_id="http-probe-skill",
                title="HTTP Probe Skill",
                config_schema={"type": "object", "properties": {"depth": {"type": "integer"}}},
                trust="verified",
            )
        )
        try:
            listed = await client.get(f"/v1/projects/{committed_project}/plugins")
            assert listed.status_code == 200, listed.text
            by_name = {row["name"]: row for row in listed.json()}
            assert by_name["skill.http-probe-skill"]["trust"] == "verified", by_name

            # A contribution cannot promote itself to core.
            UI_CONTRIBUTIONS.remove("http-probe-skill")
            UI_CONTRIBUTIONS.register(
                UIContribution(plugin_id="http-probe-skill", title="x", trust="core")
            )
            relisted = await client.get(f"/v1/projects/{committed_project}/plugins")
            claimed = {row["name"]: row for row in relisted.json()}
            assert claimed["skill.http-probe-skill"]["trust"] == "authored", claimed
            assert claimed["skill.http-probe-skill"]["plugin_id"] is not None
        finally:
            UI_CONTRIBUTIONS.remove("http-probe-skill")

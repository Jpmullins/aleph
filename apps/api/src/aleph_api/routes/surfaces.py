"""Surface composition API.

`GET /v1/projects/{id}/surfaces/{tab}` returns the A2UI surface JSON for
a right-panel tab. The renderer subscribes to updates via the existing
SSE channel; for Inc 4 this returns a one-shot snapshot.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Any
from uuid import UUID

import structlog
from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from starlette.responses import StreamingResponse

from aleph_a2ui.card_service import list_pinned
from aleph_a2ui.components.cards import (
    ApprovalCardProps,
    FindingCardProps,
    approval_card,
    finding_card,
)
from aleph_a2ui.components.surfaces import (
    ALEPH_V09_CATALOG_ID,
    artifacts_surface_v09,
    briefs_surface_v09,
    grounding_surface_v09,
    hypotheses_surface_v09,
    inspector_surface_v09,
    notes_surface_v09,
    settings_surface_v09,
    wiki_surface_v09,
)
from aleph_a2ui.messages import full_surface
from aleph_a2ui.pane_registry import PANE_REGISTRY
from aleph_a2ui.surface_streamer import (
    SurfaceStreamBuffer,
    data_model_patches_to_messages,
    diff_data_model,
    split_surface_messages,
)
from aleph_api.deps import PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep, assert_stream_access
from aleph_core.errors import NotFound, ValidationFailed
from aleph_db.repos.background_tasks import BACKGROUND_TASK_KINDS
from aleph_wiki.models import Citation, PageMergeProposal, SourcePage, WikiPage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["surfaces"])


@router.get("/{project_id}/panes")
async def list_pane_kinds(project_id: ProjectScopeDep) -> dict[str, Any]:
    """What surfaces this project can open.

    `ProjectScopeDep` rather than a bare `UUID`, even though the answer does not
    depend on the project yet: a URL that names a project and checks nothing is
    an existence oracle for any UUID a caller can guess, and the moment the
    registry becomes per-project (below) it also becomes a listing of another
    tenant's installed plugins. This was the one route in the API with a
    `{project_id}` and no scope resolution — `scripts/check-project-scope.sh`
    now refuses a second one.

    The rail renders from this. It is project-scoped on purpose even though the
    registry is process-wide today: once a plugin can be enabled per project,
    "what can this workbench do" stops having one global answer, and a client
    written against a global endpoint would have to be rewritten.
    """
    return {
        "panes": [
            {
                "id": k.id,
                "title": k.title,
                "icon": k.icon,
                "launchable": k.launchable,
                "params": list(k.params),
                "source": k.source,
            }
            for k in PANE_REGISTRY.all()
        ]
    }


# Poll-fallback window for the delta stream. The stream wakes on a push signal
# (any mutation for the project) and recomputes-and-diffs the tab's surface,
# emitting `updateDataModel` deltas only when the model actually changed (no diff
# → nothing emitted). Absent any push it recomputes after this many seconds as a
# self-healing safety net. Surface recompute hits Postgres harder than the
# agent-events requery (full tab rebuild), so its fallback is more conservative.
_STREAM_FALLBACK_SEC = 10.0

# Reconnect / resume / ordering (WP-4 sub-spec (a)). Every SSE message is stamped
# with a monotonic per-connection `seq` (SSE `id:` field + `seq` in the payload)
# and retained in a bounded ring so a reconnect carrying `Last-Event-ID` replays
# only what it missed (or falls back to a clean full snapshot if the gap is
# beyond the ring). Buffers are keyed by a client-connection id (`?cid=`) the
# frontend mints once per mount — `EventSource` auto-reconnect reuses the same
# URL (hence the same `cid`), so the ring survives a dropped connection. TTL
# eviction reaps abandoned connections; a soft cap bounds the registry.
_RING_SIZE = 64
_BUFFER_TTL_SEC = 300.0
_BUFFER_MAX = 4096
_STREAM_BUFFERS: dict[str, SurfaceStreamBuffer] = {}


def _sweep_buffers() -> None:
    """Evict abandoned per-connection buffers (TTL, then soft cap)."""
    now = time.monotonic()
    for key in [k for k, b in _STREAM_BUFFERS.items() if now - b.touched > _BUFFER_TTL_SEC]:
        _STREAM_BUFFERS.pop(key, None)
    if len(_STREAM_BUFFERS) > _BUFFER_MAX:
        # Drop the least-recently-touched entries down to the cap.
        for key, _ in sorted(_STREAM_BUFFERS.items(), key=lambda kv: kv[1].touched)[
            : len(_STREAM_BUFFERS) - _BUFFER_MAX
        ]:
            _STREAM_BUFFERS.pop(key, None)


def _sse(message: dict[str, Any]) -> bytes:
    """Serialize a seq-stamped message as an SSE event (with `id:` for resume)."""
    return f"id: {message['seq']}\ndata: {json.dumps(message)}\n\n".encode()


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip()
    return int(raw) if raw.lstrip("-").isdigit() else None


class SurfaceMessagesOut(BaseModel):
    """A2UI v0.9 message-list payload (Wave 4).

    Every right-panel tab is rendered through the upstream `@a2ui` v0_9
    `MessageProcessor` + `<A2uiSurface>` against the shared catalog, so each
    body carries an ordered list of server-to-client messages (`createSurface`
    + `updateComponents` [+ `updateDataModel`]) instead of the legacy
    `{tab, surface}` tree.
    """

    tab: str
    messages: list[dict[str, Any]]


async def _plugin_settings_messages(
    session: Any, project_id: UUID, plugin_id: str, surface_id: str
) -> list[dict[str, Any]]:
    """One plugin's settings screen, generated from its declared schema.

    The read path `settings_card.py` never had. It was 279 lines of working,
    unit-tested generator with no importer outside its own tests — and after
    WS-A3a gave it one, that caller was the SAVE handler, so the screen could
    only be seen by first writing to it.

    Values come from `plugin_settings`; the shape comes from the contribution.
    A plugin with no stored row renders its schema defaults, which is what a
    settings screen should do the first time it is opened.
    """
    from sqlalchemy import select as _select

    from aleph_a2ui.components.surfaces import ALEPH_V09_CATALOG_ID as _CATALOG
    from aleph_a2ui.settings_card import settings_surface
    from aleph_db.models.plugin_settings import PluginSettings
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS

    contribution = UI_CONTRIBUTIONS.get(plugin_id)
    if contribution is None:
        return _pane_message(surface_id, f"No plugin {plugin_id!r} has declared a settings screen.")

    row = (
        await session.execute(
            _select(PluginSettings).where(
                PluginSettings.project_id == project_id,
                PluginSettings.plugin_id == plugin_id,
            )
        )
    ).scalar_one_or_none()

    return settings_surface(
        plugin_id=contribution.plugin_id,
        plugin_title=contribution.title,
        config_schema=contribution.config_schema,
        catalog_id=_CATALOG,
        surface_id=surface_id,
        current=(row.values if row else None),
        description=contribution.description or None,
    )


# ---------------------------------------------------------------------------
# WS-B1 — the four panes that used to be a slide-over drawer.
#
# The web app's deleted `Drawers.tsx` was 742 lines behind `fixed inset-0`:
# project info, cost, members, model profile, per-capability bindings,
# connectors and their credentials, the action ledger, the agent-run digest and
# the signed-in account. It covered the workspace, obeyed none of the pane
# model's rules, and — the structural reason WS-B1 exists — meant a plugin's
# settings had nowhere to land, because every section was a hand-written React
# function.
#
# All of it is now DATA. `settings_surface_v09` emits one component whose only
# bound props are a title and an ordered list of sections; what a pane contains
# is decided here, on the server, and the client is a renderer per section KIND
# rather than per section. A plugin with a declared JSON Schema does not need
# even that — `_plugin_settings_messages` generates its screen from the
# declaration, which is the path `settings_card.py` has always provided.
# ---------------------------------------------------------------------------

#: Capabilities the settings screen offers a per-model binding for, in reading
#: order.
#:
#: **This list used to live in the browser** (`CAPABILITIES` in Drawers.tsx),
#: which is a client-side copy of something only the server can know: the set is
#: the `Capability` enum, the filtering is `CAPABILITY_POLICIES`, and both are
#: Python. `docs/plan.md` WS-B1's third criterion is that no such copy remains.
#:
#: It is still an EXPLICIT list rather than `list(CAPABILITY_POLICIES)`, and that
#: is deliberate: `scripts/_lib/capability_offers.py` exists because an offer
#: with no policy and a policy with no offer are different defects, and deriving
#: one from the other would make two of that sweep's three checks vacuous by
#: construction. `rerank` was offered, had a policy, had help text, and had no
#: resolver anywhere in Aleph; the sweep is what now catches that.
CAPABILITIES: tuple[str, ...] = (
    "synthesis",
    "judge",
    "page_selection",
    "extraction",
    "classification",
    "vision",
    "code",
    "embedding",
    "rerank",
)

#: One plain-language line per offered capability. Every entry in `CAPABILITIES`
#: must have one and vice versa — `tests/unit/test_capability_offers.py` fails
#: on either direction, because a capability with no description is a dropdown
#: nobody can choose for, and a description for a capability nothing offers is
#: the orphan that survived the original grep.
CAPABILITY_HELP: dict[str, str] = {
    "synthesis": "Composes briefs and wiki pages",
    "judge": "Scores eval outputs",
    "page_selection": "Picks wiki pages to answer from — needs a large context window",
    "extraction": "Pulls claims and citations out of sources",
    "classification": "Cheap routing and labelling",
    "vision": "Reads figures and scanned pages",
    "code": "Writes the sandboxed analysis code",
    "embedding": "Vectorises chunks for intra-source descent",
    "rerank": "Reorders retrieved passages before they reach the answer",
}

#: How long a hash-chain verification is reused before it is recomputed.
#:
#: `verify_project_chain` loads every ledger row for the project and rehashes
#: the chain. The surface stream rebuilds each open pane on every project
#: mutation and at least every `_STREAM_FALLBACK_SEC`, so verifying inline would
#: make having the Logs pane open a full table scan every ten seconds, forever.
#: The age of the result is rendered beside it rather than hidden — a
#: verification presented without its timestamp reads as "true now", which is a
#: claim this cache cannot make.
_CHAIN_TTL_SEC = 60.0
_CHAIN_CACHE: dict[UUID, tuple[float, dict[str, Any]]] = {}


def _price_label(m: Any) -> str:
    """`$5.50 / $27.50 per Mtok`, or a plain marker when the gateway gives none.

    Formatted here rather than in the browser because the browser must not
    decide what an unpriced model looks like: `is_priced` is the gateway's
    answer, and rendering `$0.00` for it is the made-up pricing
    `docs/decisions.md` D-pricing exists to stop.
    """
    if not getattr(m, "is_priced", False):
        return "unpriced"

    def per_m(v: str | None) -> str:
        return "—" if v is None else f"${Decimal(v) * 1_000_000:.2f}"

    return f"{per_m(m.input_per_token)} / {per_m(m.output_per_token)} per Mtok"


async def _gateway_section_data(app_state: Any) -> tuple[dict[str, Any], list[Any]]:
    """What the gateway serves, and an honest statement when it serves nothing.

    Three outcomes, and they are NOT the same thing: no gateway wired into this
    call path, a gateway that could not be reached, and a gateway that answered
    with an empty list. The drawer collapsed the last two into "Could not reach
    the model gateway"; a gateway that is up and advertises nothing is a
    configuration problem on the gateway, not a network one, and the operator
    needs to be told which.
    """
    catalog = getattr(app_state, "gateway_catalog", None) if app_state is not None else None
    if catalog is None:
        return (
            {
                "reachable": False,
                "model_count": 0,
                "note": (
                    "No model gateway is attached to this request path, so no model list "
                    "could be read. Bindings below show what is stored, not what is available."
                ),
            },
            [],
        )
    try:
        models = list(await catalog.models())
    except Exception as exc:  # any transport failure is the same answer here
        _log.warning("surfaces.gateway_unreachable", error=f"{type(exc).__name__}: {exc}")
        return (
            {
                "reachable": False,
                "model_count": 0,
                "note": (
                    f"Could not reach the model gateway ({type(exc).__name__}). Aleph ships "
                    "no built-in model list, so there is nothing to choose from until it "
                    "responds."
                ),
            },
            [],
        )
    note = ""
    if not models:
        note = (
            "The gateway responded but advertises no models. Check its configuration — "
            "capability bindings cannot be edited until it serves at least one."
        )
    return ({"reachable": True, "model_count": len(models), "note": note}, models)


async def _gateway_endpoints_section(
    session: Any, project_id: UUID, principal: Any, app_state: Any
) -> dict[str, Any]:
    """Where this project's model calls go, and whether that address answers.

    WS-MEP-5. Five routes existed under `/v1/projects/{id}/gateway-endpoints`
    and `grep -rn 'gateway-endpoints' apps/web/src` returned 0 — a table, a
    cipher, a resolver and a probe with no screen, which is the producer with
    no consumer CLAUDE.md names as this codebase's dominant defect, reproduced
    inside the change that was supposed to fix the configuration story.

    Three rules this section keeps, each of which has a failure behind it:

    * **The key is not here and cannot be.** `GatewayEndpoint.api_key_cipher`
      never leaves the server, so `has_api_key` and `key_version` are the whole
      answer. A masked hint would be a disclosure, and this pane is streamed —
      an SSE frame is not a place to put even a prefix of a credential.
    * **The write path is REST, not the ActionRouter.** Everything else in this
      pane follows "reads are bound, writes are calls" for shape; here it is a
      security property. A settings value dispatched as a card action lands in
      `card_actions` AND in the append-only ledger, which is why
      `settings_card` refuses a field that declares itself a secret. The key
      goes to `PUT /v1/projects/{id}/gateway-endpoints` and nowhere else.
    * **A non-owner is told, not shown.** The five REST routes are
      OWNER-gated; the surface stream is open to every member. Rendering the
      rows to everybody would widen who can read a project's gateway URLs,
      which is reconnaissance. `can_edit` is false and the list is empty, with
      a line saying why — the drawer's habit of taking a 403 and drawing
      nothing is what made the connectors panel look like "no connectors".

    `last_probe_error` is carried through verbatim. "Could not connect" sends
    an operator to look at the network when the gateway said `invalid api key`.
    """
    from aleph_db.models.gateway_endpoint import GatewayEndpoint
    from aleph_security.roles import ProjectRole, rank

    role = principal.role_in(project_id) if principal is not None else None
    is_owner = role is not None and rank(role) >= rank(ProjectRole.OWNER.value)

    settings = getattr(app_state, "settings", None)
    fallback = getattr(settings, "litellm_base_url", None) if settings is not None else None

    if not is_owner:
        return {
            "kind": "gateway_endpoints",
            "title": "Model gateway",
            "blurb": (
                "Which OpenAI-compatible endpoint this project's model calls go to. "
                "Reading and changing endpoints is owner-only, so this list is not "
                "shown to you — it is withheld, not empty."
            ),
            "can_edit": False,
            "endpoints": [],
            "fallback_base_url": None,
        }

    rows = list(
        (
            await session.execute(
                select(GatewayEndpoint)
                .where(GatewayEndpoint.project_id == project_id)
                .order_by(GatewayEndpoint.name)
            )
        )
        .scalars()
        .all()
    )

    return {
        "kind": "gateway_endpoints",
        "title": "Model gateway",
        "blurb": (
            "Aleph serves no models. Point it at any OpenAI-compatible endpoint here; the "
            "key is encrypted per project and is never sent back to this screen. Test "
            "connection reports what the endpoint itself said."
        ),
        "can_edit": True,
        "fallback_base_url": fallback,
        "endpoints": [
            {
                "id": str(r.id),
                "name": r.name,
                "base_url": r.base_url,
                "is_default": bool(r.is_default),
                "has_api_key": r.api_key_cipher is not None,
                "key_version": r.key_version,
                "last_probe_at": (None if r.last_probe_at is None else r.last_probe_at.isoformat()),
                "last_probe_ok": r.last_probe_ok,
                "last_probe_error": r.last_probe_error,
                "last_probe_model_count": r.last_probe_model_count,
            }
            for r in rows
        ],
    }


async def _model_profile_section(session: Any, project_id: UUID, app_state: Any) -> dict[str, Any]:
    """Profile templates, the current binding per capability, and the options.

    Every option carries the gateway's OWN numbers (`max_input_tokens` and both
    rates) so the browser can PATCH a binding without inventing any of them. The
    drawer already did this and explained why in a comment; the difference is
    that the numbers now arrive with the surface instead of being fetched by the
    view.
    """
    from aleph_db.repos import model_profile as profile_repo
    from aleph_models.discovery import capabilities_for

    current = await profile_repo.get_project_profile(session, project_id)
    templates = await profile_repo.list_templates(session)
    gateway, models = await _gateway_section_data(app_state)

    bindings: dict[str, Any] = dict(current.bindings_jsonb) if current is not None else {}
    capabilities: list[dict[str, Any]] = []
    for cap in CAPABILITIES:
        eligible = [m for m in models if cap in capabilities_for(m)]
        bound = bindings.get(cap)
        capabilities.append(
            {
                "id": cap,
                "label": cap.replace("_", " "),
                "help": CAPABILITY_HELP[cap],
                "bound": bound.get("model") if isinstance(bound, dict) else None,
                "eligible": [
                    {
                        "id": m.id,
                        "label": f"{m.id} · {_price_label(m)}",
                        "max_input_tokens": m.max_input_tokens,
                        # `str`, not the Decimal itself. A surface data model is
                        # serialised by `json.dumps` on the SSE path — not by
                        # FastAPI's encoder, which is what makes the snapshot
                        # route forgiving — and `Decimal` has no JSON
                        # representation. Sent raw, this ended the multiplexed
                        # stream mid-frame and every OTHER pane went dark with
                        # it. `ModelBindingIn` takes the rate as a string
                        # anyway, so this is also the shape the PATCH wants.
                        "input_per_token": (
                            None if m.input_per_token is None else str(m.input_per_token)
                        ),
                        "output_per_token": (
                            None if m.output_per_token is None else str(m.output_per_token)
                        ),
                    }
                    for m in eligible
                ],
            }
        )

    return {
        "kind": "model_profile",
        "title": "Model profile",
        "blurb": (
            "The template that maps each capability to a model. Switching re-embeds sources "
            "in the background if the embedding model changes. Options come from the gateway "
            "itself, filtered to the models that can actually do each job."
        ),
        "profiles": [t.name for t in templates],
        "current": current.name if current is not None else None,
        "gateway": gateway,
        "capabilities": capabilities,
    }


async def _connectors_section(
    session: Any, project_id: UUID, principal: Any, app_state: Any
) -> dict[str, Any]:
    """Data sources the researcher can search, and whether each has a key.

    Three things this never does, each for a stated reason:

    * **It does not read a plaintext key.** `key_state` says `set` or `unset`
      and nothing more. A credential's plaintext belongs in
      `ConnectorCredential`, which encrypts it; `settings_card` refuses a
      settings field that declares itself a secret for the same reason.
    * **It does not show key state to a non-owner.** `GET
      /v1/projects/{id}/connector-credentials` is OWNER-gated, and the surface
      stream is open to every member — so porting the drawer's key panel
      unconditionally would have widened who can see it. A non-owner gets
      `key_state: "unknown"` and a line saying why, which is more than the
      drawer managed: it simply took a 403 and rendered nothing.
    * **It does not match a credential to a connector by KIND.** The unique
      constraint is `(project_id, connector_id)` and `ConnectorCredential` has
      no `connector_kind` column at all. An earlier draft of this function
      keyed on one; it type-checked (the session is `Any`, so the row is `Any`)
      and it ran green, because the only project it was exercised against had
      no credentials. The first real key would have turned this pane into an
      error surface.
    """
    from aleph_connectors.credentials import ConnectorCredential
    from aleph_rks.models import Connector, ConnectorBinding
    from aleph_security.roles import ProjectRole, rank

    connectors = list(
        (await session.execute(select(Connector).order_by(Connector.kind))).scalars().all()
    )
    binding_rows = list(
        (
            await session.execute(
                select(ConnectorBinding).where(ConnectorBinding.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    by_connector = {b.connector_id: b for b in binding_rows}

    role = principal.role_in(project_id) if principal is not None else None
    is_owner = role is not None and rank(role) >= rank(ProjectRole.OWNER.value)

    creds: dict[Any, Any] = {}
    if is_owner:
        creds = {
            c.connector_id: c
            for c in (
                await session.execute(
                    select(ConnectorCredential).where(ConnectorCredential.project_id == project_id)
                )
            )
            .scalars()
            .all()
        }

    return {
        "kind": "connectors",
        "title": "Connectors",
        "blurb": (
            "Data sources the researcher can search. Enable a connector and, if it needs a "
            "key, add one here — keys are encrypted per-project and never leave the server."
        ),
        "connectors": [
            {
                "id": str(c.id),
                "kind": c.kind,
                "name": c.name,
                "requires_auth": c.requires_auth,
                "enabled": (
                    by_connector[c.id].enabled if c.id in by_connector else c.enabled_by_default
                ),
                "config": dict(by_connector[c.id].config_jsonb) if c.id in by_connector else {},
                "key_state": (("set" if c.id in creds else "unset") if is_owner else "unknown"),
                "status": _credential_status(app_state, creds.get(c.id)) if is_owner else None,
            }
            for c in connectors
        ],
    }


def _credential_status(app_state: Any, row: Any) -> str | None:
    """The credential blob's own `status`, for the connectors that carry one.

    Consensus writes `reconnect_required` here when its OAuth grant lapses, and
    that is the only unprompted signal anywhere that a connector has stopped
    working. `routes/connector_credentials.py` derives it the same way for
    `GET /connector-credentials`; the difference is that this path is reached
    only after the owner check above.

    Only the status string ever leaves this function. A failure to decrypt is
    `None` rather than an exception: a settings pane must not go dark because
    one credential was written under a key generation this process cannot read.
    """
    if row is None or app_state is None:
        return None
    from aleph_connectors.credentials import credential_cipher

    settings = getattr(app_state, "settings", None)
    if settings is None:
        return None
    try:
        cipher = credential_cipher(
            master_key=settings.aleph_credential_master_key,
            legacy_key=settings.credential_legacy_key,
        )
        plaintext = cipher.decrypt(
            project_id=row.project_id,
            cipher_blob=bytes(row.cipher_blob),
            key_version=row.key_version,
        )
        parsed = json.loads(plaintext)
    except Exception:
        return None
    if isinstance(parsed, dict):
        value = parsed.get("status")
        return str(value) if value is not None else None
    return None


def _plugins_section() -> dict[str, Any]:
    """Every plugin that declared a settings schema, as a way in to its screen.

    This is the listing the drawer could not have: opening one adds a
    `settings:plugin=<id>` pane beside this one, generated from the plugin's own
    declaration with no browser code involved.
    """
    from aleph_runtime.ui_contributions import UI_CONTRIBUTIONS

    contributions = sorted(UI_CONTRIBUTIONS.all(), key=lambda c: c.plugin_id)
    return {
        "kind": "plugins",
        "title": "Plugin settings",
        "blurb": (
            "A plugin gets a settings screen by declaring a config schema — it ships no "
            "browser code. Opening one puts it on the board beside this pane."
        ),
        "plugins": [
            {
                "id": c.plugin_id,
                "title": c.title,
                "description": c.description,
                "trust": c.trust,
            }
            for c in contributions
        ],
    }


async def _cost_rollup(session: Any, project_id: UUID) -> tuple[str, list[dict[str, Any]]]:
    from aleph_db.repos import cost as cost_repo

    total = await cost_repo.total_cost(session, project_id)
    by_phase = [
        {"key": k, "cost_usd": str(c), "call_count": n}
        for k, c, n in await cost_repo.cost_by_phase(session, project_id)
    ]
    return str(total), by_phase


async def _project_settings_messages(
    session: Any, project_id: UUID, surface_id: str, principal: Any, app_state: Any
) -> list[dict[str, Any]]:
    """The Settings pane: project, cost, members, models, connectors, plugins."""
    from aleph_db.models.identity import ProjectMember
    from aleph_db.repos import project as project_repo

    project = await project_repo.get_project(session, project_id)
    if project is None:
        return _pane_message(surface_id, f"No project {project_id}.")

    total_usd, _by_phase = await _cost_rollup(session, project_id)
    members = list(
        (
            await session.execute(
                select(ProjectMember)
                .where(ProjectMember.project_id == project_id)
                .order_by(ProjectMember.created_at)
            )
        )
        .scalars()
        .all()
    )

    sections: list[dict[str, Any]] = [
        {
            "kind": "fields",
            "title": "Project",
            "rows": [
                {"label": "Title", "value": project.title},
                {"label": "Description", "value": project.description or "—", "multiline": True},
                {"label": "Status", "value": project.status},
                {"label": "Created", "value": project.created_at.isoformat()},
            ],
        },
        {
            "kind": "fields",
            "title": "Cost",
            "rows": [{"label": "Spent (USD)", "value": f"${Decimal(total_usd):.4f}"}],
        },
        {
            "kind": "members",
            "title": "Members",
            "members": [
                {"id": str(m.id), "user_id": str(m.user_id), "role": m.role} for m in members
            ],
        },
        await _gateway_endpoints_section(session, project_id, principal, app_state),
        await _model_profile_section(session, project_id, app_state),
        await _connectors_section(session, project_id, principal, app_state),
        _plugins_section(),
    ]
    return settings_surface_v09(title="Settings", sections=sections, surface_id=surface_id)


async def _logs_messages(session: Any, project_id: UUID, surface_id: str) -> list[dict[str, Any]]:
    """The action ledger, and whether its hash chain still verifies.

    The append-only hash chain CLAUDE.md lists as a core invariant had no
    interface in the PRODUCT: `GET /v1/projects/{id}/ledger/verify` was called
    only by `audit/checks/action-ledger-hashchain.sh`, so the only way a person
    learned it had diverged was to run the audit script or call the route by
    hand. It is the first thing this pane says.

    This calls `verify_project_chain` directly rather than fetching that route.
    A surface producer does not self-fetch — it renders from bound props, and
    reaching for HTTP here would make a pane's content depend on the API being
    reachable from inside itself. One implementation, two callers: the route
    for operators and the audit check, this for the pane.
    """
    from aleph_db.models.ledger import ActionLedgerEvent
    from aleph_db.repos.ledger import verify_project_chain

    cached = _CHAIN_CACHE.get(project_id)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CHAIN_TTL_SEC:
        checked_at, chain = cached[0], dict(cached[1])
    else:
        result = await verify_project_chain(session, project_id)
        chain = {
            "ok": result.ok,
            "count": result.count,
            "first_divergence_event_id": (
                str(result.first_divergence.event_id) if result.first_divergence else None
            ),
        }
        _CHAIN_CACHE[project_id] = (now, dict(chain))
        checked_at = now
    chain["age_seconds"] = int(now - checked_at)

    rows = list(
        (
            await session.execute(
                select(ActionLedgerEvent)
                .where(ActionLedgerEvent.project_id == project_id)
                .order_by(ActionLedgerEvent.timestamp.desc())
                .limit(LEDGER_EVENT_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    sections: list[dict[str, Any]] = [
        {
            "kind": "ledger",
            "title": "Action ledger",
            "blurb": (
                "Every state change, hash-chained and append-only. The chain is re-verified "
                f"at most once every {int(_CHAIN_TTL_SEC)}s — walking it rehashes every row."
            ),
            "chain": chain,
            "limit": LEDGER_EVENT_LIMIT,
            "events": [
                {
                    "id": str(e.id),
                    "actor_kind": e.actor_kind,
                    "action_kind": e.action_kind,
                    "target_kind": e.target_kind,
                    "target_id": str(e.target_id) if e.target_id else None,
                    "trace_id": e.trace_id,
                    "timestamp": e.timestamp.isoformat(),
                }
                for e in rows
            ],
        }
    ]
    return settings_surface_v09(title="Logs", sections=sections, surface_id=surface_id)


async def _notifications_messages(
    session: Any, project_id: UUID, surface_id: str
) -> list[dict[str, Any]]:
    """Agent runs, failures first.

    Deliberately NOT folded into the Inspector, which is the richer pane and the
    obvious candidate. The Inspector answers "what did THIS run do"; it lists
    runs so you can pick one. This answers "is anything broken right now", which
    is a different question with a different shape — the failures are grouped
    and lead with their error text rather than being one row among fifty. WS-B1
    permits deleting a drawer section that holds nothing; this one holds the
    only unprompted statement in the app that a background job died.
    """
    from aleph_db.models.agent import AgentRun

    rows = list(
        (
            await session.execute(
                select(AgentRun)
                .where(AgentRun.project_id == project_id)
                .order_by(AgentRun.created_at.desc())
                .limit(NOTIFICATION_RUN_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    sections: list[dict[str, Any]] = [
        {
            "kind": "runs",
            "title": "Agent runs",
            "limit": NOTIFICATION_RUN_LIMIT,
            "runs": [
                {
                    "id": str(r.id),
                    "agent_kind": r.agent_kind,
                    "status": r.status,
                    "error_text": r.error_text,
                    "created_at": r.created_at.isoformat(),
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                }
                for r in rows
            ],
        }
    ]
    return settings_surface_v09(title="Notifications", sections=sections, surface_id=surface_id)


async def _profile_messages(
    session: Any, project_id: UUID, surface_id: str, principal: Any
) -> list[dict[str, Any]]:
    """Who is signed in, and what this project has spent on their behalf."""
    if principal is None:
        account_rows = [
            {
                "label": "Account",
                "value": (
                    "No principal reached this call path, so the signed-in identity could "
                    "not be resolved."
                ),
                "multiline": True,
            }
        ]
    else:
        account_rows = [
            {"label": "Email", "value": principal.email or "—"},
            {"label": "Subject", "value": principal.subject, "mono": True},
            {"label": "Actor kind", "value": principal.actor_kind},
            {"label": "User ID", "value": str(principal.user_id), "mono": True},
        ]

    total_usd, by_phase = await _cost_rollup(session, project_id)
    usage_rows = [{"label": "Spent to date", "value": f"${Decimal(total_usd):.4f}"}]
    usage_rows += [
        {
            "label": b["key"],
            "value": f"${Decimal(b['cost_usd']):.4f} · {b['call_count']} calls",
        }
        for b in by_phase
    ]

    sections: list[dict[str, Any]] = [
        {"kind": "fields", "title": "Signed in as", "rows": account_rows},
        {"kind": "fields", "title": "Usage", "rows": usage_rows},
    ]
    return settings_surface_v09(title="Profile", sections=sections, surface_id=surface_id)


def _pane_message(surface_id: str, message: str) -> list[dict[str, Any]]:
    """A one-component pane that says something.

    Used for the empty and unknown states rather than returning `[]`: an empty
    message list renders as a blank block, which is indistinguishable from a
    pane that failed to load.
    """
    return full_surface(
        surface_id=surface_id,
        catalog_id=ALEPH_V09_CATALOG_ID,
        components=[{"id": "root", "component": "Text", "text": {"path": "/message"}}],
        data_model={"message": message},
    )


def _pane_error_surface(surface_id: str, tab: str, exc: BaseException) -> list[dict[str, Any]]:
    """One pane that says it broke, so the other panes can carry on.

    Names the pane and the exception CLASS, not the message: an exception string
    can carry anything the failing code put in it, and a surface is rendered in
    a browser. The full traceback is in the API log under the same pane id.
    """
    return full_surface(
        surface_id=surface_id,
        catalog_id=ALEPH_V09_CATALOG_ID,
        components=[
            {
                "id": "root",
                "component": "Text",
                "text": {"path": "/message"},
            }
        ],
        data_model={
            "message": (
                f"The {tab!r} pane failed to load ({type(exc).__name__}). "
                "The rest of the workspace is unaffected; the details are in the "
                "API log under this pane's id."
            )
        },
    )


async def _build_tab_messages(
    session: Any,
    project_id: UUID,
    tab_lc: str,
    params: dict[str, str] | None = None,
    surface_id: str | None = None,
    *,
    principal: Any = None,
    app_state: Any = None,
) -> list[dict[str, Any]]:
    """Build the v0.9 message list for `tab_lc`. Shared by the snapshot route
    and the delta stream so both compute identical surfaces. `surface_id`
    defaults to `tab_lc` so the stamped delta `surfaceId` matches
    `createSurface`.

    `params` carries whatever the pane DECLARED, keyed by its own name — so the
    grounding pane gets `claim_id` and the Inspector gets `run_id`, rather than
    both reaching in for a positional called `page_id`.

    `principal` and `app_state` are KEYWORD-ONLY and default to `None` because
    most panes are a pure function of (session, project, params) and every
    existing caller passes only those. The two that are not: `profile` renders
    the signed-in identity, which is not project data and lives on the
    principal; and `settings` offers the models the gateway serves, which is
    `app.state.gateway_catalog` (TTL-cached, so a rebuild is a cache hit).
    Absent either, the affected SECTION says so in words — it does not render
    empty, because an empty account panel and an unauthenticated one look
    identical.
    """
    sid = surface_id or tab_lc
    args = params or {}
    page_id = args.get("page_id")

    # A registered builder wins. This is the seam `PANE_REGISTRY.extend()` has
    # always advertised and never had: the thing that BUILT a pane was the
    # if/elif chain below, which raised `NotFound` on any name it did not know,
    # so a plugin could register a pane and the app would break on it.
    #
    # The core panes keep resolving by name below rather than being converted
    # wholesale — that is a mechanical change to seven builders with no test
    # behind the move, and the point of this workstream is that a PLUGIN can add
    # one, not that the existing ones are rewritten today.
    kind = _pane_kinds().get(tab_lc)
    builder = getattr(kind, "builder", None) if kind is not None else None
    if builder is not None:
        return await builder(session, project_id, args, sid)

    if tab_lc == "wiki":
        return await _wiki_messages(session, project_id, page_id, sid)
    # "library" is the renamed Artifacts tab (ingested Sources + built
    # Artifacts). "artifacts" is kept as an alias for older client/agent nav.
    if tab_lc in ("library", "artifacts"):
        return await _library_messages(session, project_id, sid)
    if tab_lc == "notes":
        return await _notes_messages(session, project_id, sid)
    if tab_lc == "hypotheses":
        return await _hypotheses_messages(session, project_id, sid)
    if tab_lc == "briefs":
        return await _briefs_messages(session, project_id)
    if tab_lc == "grounding":
        # `claim_id`, under its own name at last.
        return await _grounding_messages(session, project_id, args.get("claim_id"), sid)
    if tab_lc == "inspector":
        return await _inspector_messages(session, project_id, args.get("run_id"), sid)
    if tab_lc == "settings":
        plugin = args.get("plugin")
        if plugin:
            return await _plugin_settings_messages(session, project_id, plugin, sid)
        return await _project_settings_messages(session, project_id, sid, principal, app_state)
    if tab_lc == "logs":
        return await _logs_messages(session, project_id, sid)
    if tab_lc == "notifications":
        return await _notifications_messages(session, project_id, sid)
    if tab_lc == "profile":
        return await _profile_messages(session, project_id, sid, principal)
    msg = f"unknown tab: {tab_lc}"
    raise NotFound(msg)


#: Surface kinds a pane may name.
#:
#: Derived from the registry rather than written out here, so the set the parser
#: accepts and the set the client is told about cannot drift — they did, and the
#: result was `artifacts` and `grounding` being streamable with nowhere on the
#: client to land. A plugin extending the registry widens both at once.
#: Runs the Inspector lists, and events it shows for one run.
#:
#: Stated, and shown in the surface, rather than silently truncating: a pane
#: quietly showing the most recent N looks identical to one showing all of them,
#: and the difference matters the moment somebody asks "did it run at all
#: yesterday".
#: Run kinds the Inspector lists. `assistant` is a chat turn; the rest are the
#: background tickets a turn can dispatch, and they belong in the same timeline
#: because the point of the pane is to show what a conversation caused.
#: Imported from the repository rather than spelled here so a new kind appears
#: in the Inspector the day it is added, which is the drift that made the
#: original `== "assistant"` filter wrong and silent.
_INSPECTOR_RUN_KINDS = ("assistant", *BACKGROUND_TASK_KINDS)

INSPECTOR_RUN_LIMIT = 50
INSPECTOR_EVENT_LIMIT = 500

#: Ledger rows the Logs pane carries, and agent runs the Notifications pane
#: carries. Both are STATED in the surface next to the list, for the reason
#: `_INSPECTOR_RUN_KINDS` gives above: a pane quietly showing the most recent N
#: looks identical to one showing all of them.
LEDGER_EVENT_LIMIT = 50
NOTIFICATION_RUN_LIMIT = 25


def _pane_kinds() -> dict[str, Any]:
    """`pane id -> PaneKind`, so the parser can read each pane's declared params.

    Was `frozenset[str]` — enough to reject an unknown tab and nothing else,
    which is why `_parse_pane_specs` could only ever hand on one hardcoded key.
    """
    return {k.id: k for k in PANE_REGISTRY.all()}


def _parse_pane_specs(raw: str) -> list[tuple[str, str, dict[str, str]]]:
    """``"wiki,inspector:run_id=abc"`` → ``[(surface_id, tab, params), …]``.

    The surface id is the spec verbatim, which is exactly the pane id the client
    mints — so a delta stamped with it lands in the right pane without any
    further mapping. Unknown tabs are dropped rather than raising: one bad pane
    in a URL must not take down the whole workspace's stream.

    **Params are passed through BY NAME, and only the ones the pane declares.**

    This used to read exactly one key, `page_id`, and hand it on as a bare
    positional. The grounding pane declares `params=("claim_id",)` and had to
    receive its claim id under the name `page_id` anyway, with an apologetic
    docstring at the far end explaining that "the `page_id` pane param carries
    the CLAIM id here". One opaque parameter with the wrong name was survivable
    with one such pane and stops being so at two.

    An undeclared param is DROPPED rather than passed. `PaneKind.params` is the
    contract; accepting anything a URL happens to carry would make the registry
    a suggestion, and a pane builder receiving a key it never declared is how a
    typo becomes a silently ignored filter.
    """
    kinds = _pane_kinds()
    out: list[tuple[str, str, dict[str, str]]] = []
    seen: set[str] = set()
    for raw_spec in raw.split(","):
        spec = raw_spec.strip()
        if not spec or spec in seen:
            continue
        tab, _, params = spec.partition(":")
        tab = tab.lower()
        kind = kinds.get(tab)
        if kind is None:
            continue
        declared = set(kind.params)
        parsed: dict[str, str] = {}
        for kv in params.split("&"):
            k, _, v = kv.partition("=")
            if v and k in declared:
                parsed[k] = v
        seen.add(spec)
        out.append((spec, tab, parsed))
    return out


@router.get("/{project_id}/surfaces/stream", response_model=None)
async def stream_surfaces_multiplexed(
    project_id: Annotated[UUID, Path(...)],
    request: Request,
    principal: PrincipalDep,
    panes: str = Query(default="wiki"),
) -> StreamingResponse:
    """One SSE connection carrying deltas for EVERY open pane.

    The workspace is a set of panes, not one surface, and a connection per pane
    hits the browser's ~6-per-origin HTTP/1.1 cap at four panes — with two other
    Aleph streams already open. This multiplexes them.

    It is also *stronger* than one stream per pane, not merely cheaper:
    `SurfaceStreamBuffer.stamp()` issues one monotonic `seq` per connection, so
    multiplexed panes share a single total order. Independent connections each
    have their own `seq` space and give no cross-pane ordering at all — a page
    and the claim view beside it could render mutually inconsistent states.

    The A2UI protocol was built for this: every message carries `surfaceId`, and
    the client's `MessageProcessor` already holds a `surfacesMap`. One surface
    per connection was a UI constraint, never a protocol one.
    """
    await assert_stream_access(request, project_id, principal)
    specs = _parse_pane_specs(panes)
    if not specs:
        specs = [("wiki", "wiki", {})]

    maker = request.app.state.session_maker
    broker = request.app.state.change_broker
    raw_cid = request.query_params.get("cid")
    # Buffer key includes the pane set: resuming with a different set of panes
    # must not replay another layout's buffered bytes.
    cid = f"{project_id}:{panes}:{raw_cid}" if raw_cid else None
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))

    async def _gen() -> AsyncIterator[bytes]:
        _sweep_buffers()
        buf = _STREAM_BUFFERS.get(cid) if cid else None

        async def _build_all() -> dict[str, tuple[list[dict[str, Any]], Any]]:
            out: dict[str, tuple[list[dict[str, Any]], Any]] = {}
            async with maker() as session:
                for surface_id, tab, pane_params in specs:
                    try:
                        msgs = await _build_tab_messages(
                            session,
                            project_id,
                            tab,
                            pane_params,
                            surface_id,
                            principal=principal,
                            app_state=request.app.state,
                        )
                        # Serialise here, inside the per-pane guard, and throw
                        # the bytes away.
                        #
                        # `_sse` calls `json.dumps` far below this loop, in the
                        # generator that feeds the socket — so a value with no
                        # JSON form does not fail the pane that produced it, it
                        # ends the WHOLE multiplexed stream and blanks every
                        # open pane. That is exactly the outcome the except
                        # branch below exists to prevent, arriving through a
                        # door the guard did not cover. It happened: a
                        # `Decimal` gateway rate in the settings model killed
                        # the connection after two frames, with the traceback
                        # only in the API's stderr and the browser showing
                        # panes stuck on "waiting for the first frame".
                        #
                        # The cost is one extra serialisation per pane per
                        # rebuild, which is the same work `_sse` is about to do.
                        json.dumps(msgs)
                    except Exception as exc:
                        # One pane's failure must not take the workspace down.
                        #
                        # This loop feeds the SINGLE multiplexed connection that
                        # every open pane reads from, and an exception escaping
                        # here ended the generator — so one broken pane blanked
                        # all of them, with the reason only in the API's stderr.
                        # That is the specific thing that makes "a plugin can add
                        # a pane" unsafe: a plugin's bug becomes an outage.
                        _log.exception(
                            "surfaces.pane_build_failed",
                            pane=tab,
                            surface_id=surface_id,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        msgs = _pane_error_surface(surface_id, tab, exc)
                    out[surface_id] = split_surface_messages(msgs)
            return out

        current = await _build_all()

        if buf is None:
            buf = SurfaceStreamBuffer(_RING_SIZE)
            if cid:
                _STREAM_BUFFERS[cid] = buf

        resumable = last_event_id is not None and buf.can_replay(last_event_id) and buf.model
        if resumable:
            for m in buf.messages_after(last_event_id):
                yield _sse(m)
        else:
            for surface_id, _tab, _pid in specs:
                structural, _model = current[surface_id]
                for m in structural:
                    yield _sse(buf.stamp(m))
                for surface_id2, (_s, model) in current.items():
                    if surface_id2 != surface_id:
                        continue
                    for delta in data_model_patches_to_messages(
                        surface_id=surface_id, patches=diff_data_model({}, model), next_model=model
                    ):
                        yield _sse(buf.stamp(delta))

        # Per-surface previous state, so each pane diffs against its own model.
        prev: dict[str, tuple[list[dict[str, Any]], Any]] = current
        buf.structural = [m for s, _ in current.values() for m in s]
        buf.model = {k: v[1] for k, v in current.items()}

        async with broker.subscribe(project_id) as sub:
            while True:
                if await request.is_disconnected():
                    return
                await sub.wait(timeout=_STREAM_FALLBACK_SEC)
                # Coalesce a burst: one ingest writes many ledger rows, and
                # rebuilding every pane per row is the amplification this
                # endpoint exists to avoid.
                sub.drain()
                if await request.is_disconnected():
                    return

                current = await _build_all()
                for surface_id, (structural, model) in current.items():
                    prev_structural, prev_model = prev[surface_id]
                    if structural != prev_structural:
                        for m in structural:
                            if "updateComponents" in m:
                                yield _sse(buf.stamp(m))
                    for delta in data_model_patches_to_messages(
                        surface_id=surface_id,
                        patches=diff_data_model(prev_model, model),
                        next_model=model,
                    ):
                        yield _sse(buf.stamp(delta))
                prev = current
                buf.model = {k: v[1] for k, v in current.items()}
                yield b": heartbeat\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _params_from_query(tab_lc: str, request_params: Any) -> dict[str, str]:
    """Every param the named pane DECLARES, read off the query string by name.

    The single-surface routes took `page_id` as an explicit query parameter, so
    a pane declaring anything else could not be addressed through them at all —
    the Inspector's `run_id` would have been silently dropped and it would have
    rendered "no run selected" for every run. `page_id` stays accepted for the
    panes that declare it; nothing else changes for them.
    """
    kind = _pane_kinds().get(tab_lc)
    if kind is None:
        return {}
    out: dict[str, str] = {}
    for name in kind.params or ("page_id",):
        value = request_params.get(name)
        if value:
            out[name] = str(value)
    return out


@router.get("/{project_id}/surfaces/{tab}", response_model=None)
async def get_surface(
    project_id: ProjectScopeDep,
    tab: str,
    session: SessionDep,
    request: Request,
    principal: PrincipalDep,
) -> SurfaceMessagesOut:
    tab_lc = tab.lower()
    messages = await _build_tab_messages(
        session,
        project_id,
        tab_lc,
        _params_from_query(tab_lc, request.query_params),
        principal=principal,
        app_state=request.app.state,
    )
    return SurfaceMessagesOut(tab=tab_lc, messages=messages)


@router.get("/{project_id}/surfaces/{tab}/stream", response_model=None)
async def stream_surface(
    project_id: Annotated[UUID, Path(...)],
    tab: str,
    request: Request,
    principal: PrincipalDep,
) -> StreamingResponse:
    """Delta SurfaceStreamer with reconnect/resume + ordering (WP-4 sub-spec a).

    On a fresh connect, emits the full v0_9 surface for `tab` (`createSurface` /
    `updateComponents` / root `updateDataModel`), each stamped with a monotonic
    `seq` (SSE `id:` + `seq` in the payload). Then, on every LISTEN/NOTIFY wake
    (with a `_STREAM_FALLBACK_SEC` self-heal poll), it rebuilds and emits:

    * an `updateComponents` message *iff* the structural component list changed
      (the processor updates existing ids in place, adds only new ones); and
    * one `updateDataModel` per minimal `diff_data_model` patch, so a bound prop
      change (e.g. a hypothesis's confidence) re-renders only that prop.

    **Reconnect.** The browser `EventSource` reconnects to the same URL (same
    `?cid=`) carrying `Last-Event-ID`. If that id is still within this
    connection's ring, we replay only the retained tail and then forward-diff
    from the model the client last had to current DB state — delivering exactly
    the deltas missed while disconnected, never a resnapshot. If the id is
    beyond the ring (or the buffer is gone), we send a clean full snapshot with
    a fresh baseline seq. The client applies messages in `seq` order and drops
    duplicates/out-of-order ids.
    """
    # Membership check WITHOUT pinning a pool connection for the stream's life.
    await assert_stream_access(request, project_id, principal)
    tab_lc = tab.lower()
    surface_id = tab_lc
    maker = request.app.state.session_maker
    broker = request.app.state.change_broker
    # Namespace the buffer key by (project, tab, cid): a `cid` leaked from the
    # URL (query params reach logs/proxies/history) can then only ever resume
    # the exact project+tab stream that created it — never another project's
    # buffered surface bytes (wiki body_md, claims, notes, …). The membership
    # check above (assert_stream_access) gates the project; this gates replay.
    pane_params = _params_from_query(tab_lc, request.query_params)
    raw_cid = request.query_params.get("cid")
    cid = f"{project_id}:{tab_lc}:{raw_cid}" if raw_cid else None
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))

    async def _gen() -> AsyncIterator[bytes]:
        _sweep_buffers()
        buf = _STREAM_BUFFERS.get(cid) if cid else None

        async with maker() as session:
            fresh = await _build_tab_messages(
                session,
                project_id,
                tab_lc,
                pane_params,
                surface_id,
                principal=principal,
                app_state=request.app.state,
            )
        structural, model = split_surface_messages(fresh)

        if buf is not None and last_event_id is not None and buf.can_replay(last_event_id):
            # Resume: replay the retained tail (original seqs), then forward-diff
            # only what changed while the client was disconnected.
            for m in buf.messages_after(last_event_id):
                yield _sse(m)
            if structural != buf.structural:
                for m in structural:
                    if "updateComponents" in m:
                        yield _sse(buf.stamp(m))
            patches = diff_data_model(buf.model, model)
            for delta in data_model_patches_to_messages(
                surface_id=surface_id, patches=patches, next_model=model
            ):
                yield _sse(buf.stamp(delta))
            buf.structural, buf.model = structural, model
        else:
            # Fresh full snapshot (new baseline seq).
            if buf is None:
                buf = SurfaceStreamBuffer(_RING_SIZE)
                if cid:
                    _STREAM_BUFFERS[cid] = buf
            for m in fresh:
                yield _sse(buf.stamp(m))
            buf.structural, buf.model = structural, model

        prev_structural, prev_model = buf.structural, buf.model

        # Push: any mutation for this project wakes a recompute-and-diff the
        # instant it commits (the broker is fed by the LISTEN/NOTIFY listener).
        # `sub.wait` also returns after `_STREAM_FALLBACK_SEC` with no push, so a
        # dropped listener self-heals.
        async with broker.subscribe(project_id) as sub:
            while True:
                if await request.is_disconnected():
                    return
                await sub.wait(timeout=_STREAM_FALLBACK_SEC)
                if await request.is_disconnected():
                    return

                async with maker() as session:
                    fresh = await _build_tab_messages(
                        session,
                        project_id,
                        tab_lc,
                        pane_params,
                        surface_id,
                        principal=principal,
                        app_state=request.app.state,
                    )
                structural, model = split_surface_messages(fresh)

                if structural != prev_structural:
                    for m in structural:
                        if "updateComponents" in m:
                            yield _sse(buf.stamp(m))
                    prev_structural = structural
                    buf.structural = structural

                patches = diff_data_model(prev_model, model)
                for delta in data_model_patches_to_messages(
                    surface_id=surface_id, patches=patches, next_model=model
                ):
                    yield _sse(buf.stamp(delta))
                prev_model = model
                buf.model = model

                # Heartbeat so idle proxies don't close the connection.
                yield b": heartbeat\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _hypotheses_messages(
    session: Any, project_id: UUID, surface_id: str
) -> list[dict[str, Any]]:
    """Data-bound Hypotheses tab: the tracked-hypothesis list + the ACH matrix,
    loaded through the existing hypotheses routes (same queries the REST list /
    `/hypotheses/ach` endpoints use) and bound into the surface data model."""
    from aleph_api.routes.hypotheses import get_ach_matrix, get_hypotheses

    items = [h.model_dump(mode="json") for h in await get_hypotheses(project_id, session)]
    ach_out = (await get_ach_matrix(project_id, session)).model_dump(mode="json")
    # ACH is only meaningful once there is evidence; expose null otherwise so the
    # view renders its empty state rather than an empty grid.
    ach: dict[str, Any] | None = ach_out if ach_out.get("targets") else None
    return hypotheses_surface_v09(items=items, ach=ach, surface_id=surface_id)


async def _library_messages(
    session: Any, project_id: UUID, surface_id: str
) -> list[dict[str, Any]]:
    """Data-bound Library tab: ingested Sources + built Artifacts, loaded through
    the existing sources/artifacts routes and bound into the surface data model.

    Each source carries a bound ``normalized_preview`` — the head of its
    normalized text (WP-4e) — so `SourceCard` renders the preview in place with
    NO self-fetch. Previews come from the first `DocumentChunk` per source (in
    Postgres, one batched query — never an asset-store read per source and no
    N+1)."""
    from aleph_api.routes.artifacts import get_artifacts
    from aleph_api.routes.sources import list_sources

    sources = [s.model_dump(mode="json") for s in await list_sources(project_id, session)]
    previews = await _source_previews(session, project_id, [UUID(s["id"]) for s in sources])
    for s in sources:
        s["normalized_preview"] = previews.get(UUID(s["id"]))
    artifacts = [a.model_dump(mode="json") for a in await get_artifacts(project_id, session)]
    await _annotate_drift(session, artifacts)
    return artifacts_surface_v09(sources=sources, artifacts=artifacts, surface_id=surface_id)


async def _annotate_drift(session: Any, artifacts: list[dict[str, Any]]) -> None:
    """Stamp a live-computed ``drifted`` flag onto each artifact dict (WP-6 §5).

    An artifact is drifted iff any upstream wiki page recorded in its current
    version's ``lineage_jsonb["source_pages"]`` now has a newer current revision
    than the one the build recorded. No stored flag — always live-computed off
    the current wiki graph. Two batched queries (versions, then pages)."""
    from typing import cast

    from aleph_artifacts.drift import is_drifted
    from aleph_artifacts.models import ArtifactVersion

    version_ids = [UUID(a["current_version_id"]) for a in artifacts if a.get("current_version_id")]
    source_pages_by_version: dict[UUID, list[dict[str, Any]]] = {}
    all_page_ids: set[UUID] = set()
    if version_ids:
        versions = list(
            (
                await session.execute(
                    select(ArtifactVersion).where(ArtifactVersion.id.in_(version_ids))
                )
            )
            .scalars()
            .all()
        )
        for av in versions:
            lineage = cast("dict[str, Any]", av.lineage_jsonb or {})
            sps = cast("list[dict[str, Any]]", lineage.get("source_pages") or [])
            source_pages_by_version[av.id] = sps
            for sp in sps:
                pid_val = sp.get("page_id")
                if pid_val:
                    all_page_ids.add(UUID(str(pid_val)))
    current_revs: dict[UUID, UUID | None] = {}
    if all_page_ids:
        for pid_row, rev_row in (
            await session.execute(
                select(WikiPage.id, WikiPage.current_revision_id).where(
                    WikiPage.id.in_(all_page_ids)
                )
            )
        ).all():
            current_revs[pid_row] = rev_row
    for a in artifacts:
        cvid = a.get("current_version_id")
        sps = source_pages_by_version.get(UUID(cvid)) if cvid else None
        a["drifted"] = is_drifted(sps, current_revs)


# Normalized-text preview length (chars). The Library builder binds this head of
# each source's first chunk into `SourceCard.normalized_preview`; the reader shows
# more only by opening the raw asset (an iframe URL, not a fetch).
_SOURCE_PREVIEW_CHARS = 2000


async def _source_previews(
    session: Any, project_id: UUID, source_ids: list[UUID]
) -> dict[UUID, str]:
    """Batched first-chunk (`ordinal == 0`) text per source, truncated to a
    bounded preview. One query for all sources — no N+1, no asset-store read."""
    from aleph_rks.models import DocumentChunk

    if not source_ids:
        return {}
    rows = (
        await session.execute(
            select(DocumentChunk.source_id, DocumentChunk.text).where(
                DocumentChunk.project_id == project_id,
                DocumentChunk.source_id.in_(source_ids),
                DocumentChunk.ordinal == 0,
            )
        )
    ).all()
    out: dict[UUID, str] = {}
    for sid, text in rows:
        out[sid] = text[:_SOURCE_PREVIEW_CHARS] if text else ""
    return out


async def _wiki_messages(
    session: Any, project_id: UUID, page_id: str | None, surface_id: str
) -> list[dict[str, Any]]:
    """Data-bound Wiki tab: the page-browser list, plus the open page's reader
    payload when `?page_id=` is set. Both come from the existing wiki routes
    (`list_pages` / `get_page`) so the surface renders exactly what the REST
    endpoints return, minus the client fetch. `open` is null when browsing."""
    from aleph_api.routes.wiki import get_page, list_pages

    pages = [p.model_dump(mode="json") for p in await list_pages(project_id, session)]
    # WP-6 F4: derive the `retracted` marker (page has ≥1 retracted-confidence
    # claim on its current revision) for every listed page in one query, stamp it
    # onto each row, then sort the list by freshness (freshest first; unscored
    # pages last) so the badge + ordering read as a trust surface.
    retracted_pages = await _retracted_page_ids(session, project_id, [UUID(p["id"]) for p in pages])
    for p in pages:
        p["retracted"] = UUID(p["id"]) in retracted_pages
    pages.sort(key=lambda p: (p.get("freshness") is None, -(p.get("freshness") or 0), p["title"]))
    open_page: dict[str, Any] | None = None
    if page_id:
        try:
            pid = UUID(page_id)
        except ValueError as exc:
            msg = "invalid page_id"
            raise ValidationFailed(msg) from exc
        try:
            detail = (await get_page(project_id, pid, session)).model_dump(mode="json")
        except NotFound:
            detail = None
        if detail is not None:
            claims = detail["claims"]
            citations = await _resolve_citations(session, project_id, claims)
            open_page = {
                "page_id": detail["page"]["id"],
                "title": detail["page"]["title"],
                "status": detail["page"]["status"],
                "is_stub": detail["page"]["is_stub"],
                # WP-6 trust layer: the reader's freshness badge + retracted
                # banner read these off page_meta (freshness/volatility/
                # verified_at come from the page row; `retracted` is derived from
                # having ≥1 retracted-confidence claim).
                "freshness": detail["page"]["freshness"],
                "volatility": detail["page"]["volatility"],
                "verified_at": detail["page"]["verified_at"],
                "retracted": bool(await _retracted_page_ids(session, project_id, [pid])),
                "revision": detail["revision"],
                "claims": claims,
                # Resolved [cN] markers → source title + url for the reader's
                # citation popover (WP-4b).
                "citations": citations,
                "wikilinks_out": detail["wikilinks_out"],
                # Deterministic server-compiled HTML doc (WP-4b). Bound into
                # HtmlDocCard's sandboxed iframe src; the card never fetches.
                "html_url": f"/v1/projects/{project_id}/wiki/pages/{pid}/html",
            }
    # The schema's categories, so the browser can group and title its sections
    # without a second round-trip, and the lint's severity counts so the header
    # can state the corpus's health. Counts only — the findings are a separate
    # read, since 300 of them in every surface push would make this payload
    # mostly a list nobody asked for.
    from aleph_wiki.lint import lint_wiki
    from aleph_wiki.schema_service import SchemaService

    schema = await SchemaService(session).get(project_id)
    report = await lint_wiki(session, project_id=project_id, schema=schema)
    categories = [{"id": c.id, "title": c.title, "blurb": c.blurb} for c in schema.categories]
    health = {
        "pages_scanned": report.pages_scanned,
        "stubs_skipped": report.stubs_skipped,
        "total": len(report.findings),
        "by_severity": report.by_severity,
        "by_check": report.by_check,
    }
    return wiki_surface_v09(
        pages=pages,
        open_page=open_page,
        categories=categories,
        health=health,
        surface_id=surface_id,
    )


async def _retracted_page_ids(session: Any, project_id: UUID, page_ids: list[UUID]) -> set[UUID]:
    """Pages (of ``page_ids``) carrying ≥1 retracted-confidence claim on their
    current revision. One query; drives the WP-6 `retracted` reader marker.

    A retraction (``aleph_reviewer.retraction.retract_source``) sets the
    dependent claims' ``status="retracted"``; a page is marked retracted iff
    such a claim exists on the page's *current* revision."""
    from aleph_wiki.models import WikiClaim

    if not page_ids:
        return set()
    rows = (
        await session.execute(
            select(WikiClaim.page_id)
            .join(WikiPage, WikiPage.id == WikiClaim.page_id)
            .where(
                WikiClaim.project_id == project_id,
                WikiClaim.page_id.in_(page_ids),
                WikiClaim.status == "retracted",
                WikiClaim.revision_id == WikiPage.current_revision_id,
            )
            .distinct()
        )
    ).all()
    return {pid for (pid,) in rows}


async def _resolve_citations(
    session: Any, project_id: UUID, claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Resolve the `Citation` rows for a page's claims to source title + url.

    Two queries (citations, then source pages/sources) — no N+1. Each entry is
    ``{marker, claim_id, source_page_id, source_title, url, chunk_ids}``; the
    reader keys its `[cN]` popover on `marker`."""
    from aleph_rks.models import Source

    claim_ids = [UUID(c["id"]) for c in claims if c.get("id")]
    if not claim_ids:
        return []
    cite_rows = list(
        (await session.execute(select(Citation).where(Citation.claim_id.in_(claim_ids))))
        .scalars()
        .all()
    )
    if not cite_rows:
        return []
    # `Citation.source_page_id` is a `source_pages` PK — the same id-space the
    # retraction blast radius, freshness, the refresh job and the mechanical
    # reviewer all resolve it in. This reader previously treated it as a
    # `wiki_pages` id; the two never disagreed only because the column was
    # always NULL. Resolving it the wrong way would silently return
    # `source_title: null, url: null` for every citation.
    source_page_ids = {c.source_page_id for c in cite_rows if c.source_page_id is not None}
    titles: dict[UUID, str] = {}
    urls: dict[UUID, str | None] = {}
    wiki_page_of: dict[UUID, UUID] = {}
    if source_page_ids:
        sp_rows = list(
            (
                await session.execute(
                    select(SourcePage.id, SourcePage.page_id, Source.title, Source.url)
                    .join(Source, Source.id == SourcePage.source_id)
                    .where(SourcePage.id.in_(source_page_ids))
                )
            ).all()
        )
        for sp_id, page_id_, src_title, src_url in sp_rows:
            wiki_page_of[sp_id] = page_id_
            urls[sp_id] = src_url
            titles[sp_id] = src_title
        # Prefer the wiki page's own title when it has one.
        page_titles = dict(
            (
                await session.execute(
                    select(WikiPage.id, WikiPage.title).where(
                        WikiPage.id.in_(set(wiki_page_of.values()))
                    )
                )
            ).all()
        )
        for sp_id, page_id_ in wiki_page_of.items():
            if page_id_ in page_titles:
                titles[sp_id] = page_titles[page_id_]
    out: list[dict[str, Any]] = []
    for c in cite_rows:
        spid = c.source_page_id
        out.append(
            {
                "marker": c.citation_marker,
                "claim_id": str(c.claim_id),
                # The client gets the *wiki page* id — the only one it can
                # navigate to. The bridge PK is an internal join key.
                "source_page_id": (
                    str(wiki_page_of[spid]) if spid is not None and spid in wiki_page_of else None
                ),
                "source_title": titles.get(spid) if spid is not None else None,
                "url": urls.get(spid) if spid is not None else None,
                "chunk_ids": list(c.chunk_ids or []),
            }
        )
    return out


async def _notes_messages(session: Any, project_id: UUID, surface_id: str) -> list[dict[str, Any]]:
    """Data-bound Notes tab: each note with its first (lowest-ordinal) section's
    body, loaded in TWO queries (notes, then all sections) — no N+1. Editing a
    body is an `edit_note` action through the router; the delta patches in place.
    """
    from aleph_notes.models import Note, NoteSection

    notes = list(
        (
            await session.execute(
                select(Note).where(Note.project_id == project_id).order_by(Note.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    sections = list(
        (
            await session.execute(
                select(NoteSection)
                .where(NoteSection.project_id == project_id)
                .order_by(NoteSection.ordinal.asc())
            )
        )
        .scalars()
        .all()
    )
    first_by_note: dict[UUID, Any] = {}
    for s in sections:
        first_by_note.setdefault(s.note_id, s)
    notes_out: list[dict[str, Any]] = []
    for n in notes:
        first = first_by_note.get(n.id)
        notes_out.append(
            {
                "id": str(n.id),
                "title": n.title,
                "section_id": str(first.id) if first is not None else None,
                "body_md": first.body_md if first is not None else "",
                "updated_at": n.updated_at.isoformat() if n.updated_at else None,
            }
        )
    return notes_surface_v09(notes=notes_out, surface_id=surface_id)


async def _briefs_messages(session: Any, project_id: UUID) -> list[dict[str, Any]]:
    """v0.9 message list for the Briefs tab — a single `BriefsSurface`.

    The action pile: pending `SynthesisProposal`s render as `ApprovalCard`s and
    open `ReviewFinding`s as `FindingCard`s (the legacy `A2UIComponent`
    `{type,id,props}` shape the surface view consumes). Badge = total pending items.
    """
    from aleph_connectors.models import SynthesisProposal
    from aleph_reviewer.models import ReviewFinding

    rows = list(
        (
            await session.execute(
                select(SynthesisProposal).where(
                    SynthesisProposal.project_id == project_id,
                    SynthesisProposal.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    cards: list[dict[str, Any]] = []
    for p in rows:
        cards.append(
            approval_card(
                ApprovalCardProps(
                    target_id=p.id,
                    target_kind="synthesis_proposal",
                    title=f"Synthesis: {p.topic}",
                    summary=f"Approve the proposed wiki revision for “{p.topic}”.",
                    severity="info",
                ),
                card_id=f"synth-{p.id}",
            )
        )
    # Pending page-merge proposals (curator dedup) — human-gated ApprovalCards.
    merges = list(
        (
            await session.execute(
                select(PageMergeProposal).where(
                    PageMergeProposal.project_id == project_id,
                    PageMergeProposal.status == "pending",
                )
            )
        )
        .scalars()
        .all()
    )
    for mp in merges:
        titles = dict(
            (
                await session.execute(
                    select(WikiPage.id, WikiPage.title).where(
                        WikiPage.id.in_([mp.source_page_id, mp.target_page_id])
                    )
                )
            ).all()
        )
        src = titles.get(mp.source_page_id, "source")
        tgt = titles.get(mp.target_page_id, "target")
        cards.append(
            approval_card(
                ApprovalCardProps(
                    target_id=mp.id,
                    target_kind="page_merge_proposal",
                    title=f"Merge: “{src}” → “{tgt}”",
                    summary=(
                        f"The curator thinks “{src}” duplicates “{tgt}”. Approve to merge "
                        f"(redirect links, rewrite references, retire the duplicate). "
                        f"{mp.rationale}"
                    )[:1000],
                    severity="high",
                ),
                card_id=f"merge-{mp.id}",
            )
        )
    findings = list(
        (
            await session.execute(
                select(ReviewFinding)
                .where(
                    ReviewFinding.project_id == project_id,
                    ReviewFinding.status == "open",
                )
                .order_by(ReviewFinding.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    for f in findings:
        cards.append(
            finding_card(
                FindingCardProps(
                    finding_id=f.id,
                    severity=f.severity,
                    kind=f.finding_kind,
                    summary=f"{f.title} — {f.description}"[:1000],
                    evidence_refs=list(f.evidence_refs_jsonb or []),
                ),
                card_id=f"finding-{f.id}",
            )
        )
    # Pinned + agent-composed cards. Spotlighted cards (WP-4d) are ordered first
    # across the whole pile and carry a `spotlight: true` flag in their props.
    spotlighted: list[dict[str, Any]] = []
    normal_pinned: list[dict[str, Any]] = []
    for card, version in await list_pinned(session, project_id=project_id, pinned_to="briefs"):
        payload: dict[str, Any] = dict(version.a2ui_payload_jsonb)
        if card.spotlighted:
            props: dict[str, Any] = dict(payload.get("props") or {})
            props["spotlight"] = True
            payload["props"] = props
            spotlighted.append(payload)
        else:
            normal_pinned.append(payload)
    ordered = spotlighted + cards + normal_pinned
    return briefs_surface_v09(badge_count=len(ordered), children=ordered)


async def _inspector_messages(
    session: Any, project_id: UUID, run_id: str | None, surface_id: str
) -> list[dict[str, Any]]:
    """The project's assistant runs, and the timeline of the selected one.

    Reads `agent_runs` and `agent_events` — the tables WS-C3a started writing on
    the chat path. Before that, seventeen producers wrote `AgentRun` rows and not
    one of them was a conversation, so this pane would have rendered an
    authoritative-looking empty list for every project.

    Runs are capped and ordered newest-first. The cap is stated rather than
    silent: a pane that quietly shows the most recent N looks identical to one
    showing all of them, and the difference matters the moment somebody asks
    "did it run at all yesterday".
    """
    from sqlalchemy import select as _select

    from aleph_db.models.agent import AgentEvent, AgentRun

    rows = list(
        (
            await session.execute(
                _select(AgentRun)
                .where(
                    AgentRun.project_id == project_id,
                    # Chat turns AND the background tickets they dispatch. The
                    # filter was `== "assistant"`, and a ticket's agent_kind is
                    # its job kind (`reindex_corpus`, `review_sweep`), so no
                    # ticket could ever appear here and there was no way to
                    # select one. Reusing `agent_runs` instead of a new table
                    # was justified in two production docstrings by "the
                    # Inspector reads it with no change"; it did not.
                    AgentRun.agent_kind.in_(_INSPECTOR_RUN_KINDS),
                )
                .order_by(AgentRun.started_at.desc().nullslast())
                .limit(INSPECTOR_RUN_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    def _run_dict(row: Any) -> dict[str, Any]:
        started, completed = row.started_at, row.completed_at
        return {
            "id": str(row.id),
            "status": row.status,
            "started_at": started.isoformat() if started else None,
            "completed_at": completed.isoformat() if completed else None,
            "duration_ms": (
                int((completed - started).total_seconds() * 1000) if started and completed else None
            ),
            # Truncated here rather than in the renderer: an error text is
            # arbitrary length and a surface payload is not the place to
            # discover that.
            "error_text": (row.error_text or None) if row.error_text else None,
            # Which kind of run this is, so the pane can tell a conversation
            # from a job it started rather than showing an undifferentiated
            # list of ids.
            "agent_kind": row.agent_kind,
        }

    runs = [_run_dict(r) for r in rows]

    selected_row = None
    if run_id:
        selected_row = next((r for r in rows if str(r.id) == run_id), None)
        if selected_row is None:
            # Named a run that is not in the window, or not in this project.
            # Fetching it directly would leak another project's run, so this
            # scopes the lookup rather than trusting the id.
            selected_row = (
                await session.execute(
                    _select(AgentRun).where(
                        AgentRun.id == _as_uuid(run_id),
                        AgentRun.project_id == project_id,
                    )
                )
            ).scalar_one_or_none()
    elif rows:
        # No run named: show the most recent, which is what somebody opening
        # the pane after a turn is looking for.
        selected_row = rows[0]

    events: list[dict[str, Any]] = []
    if selected_row is not None:
        event_rows = list(
            (
                await session.execute(
                    _select(AgentEvent)
                    .where(AgentEvent.agent_run_id == selected_row.id)
                    .order_by(AgentEvent.timestamp)
                    .limit(INSPECTOR_EVENT_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        for event in event_rows:
            payload = event.payload_jsonb or {}
            events.append(
                {
                    "kind": event.event_kind,
                    "tool": payload.get("tool"),
                    "subagent": payload.get("subagent"),
                    "tool_call_id": payload.get("tool_call_id"),
                    "duration_ms": payload.get("duration_ms"),
                    "args": payload.get("args"),
                    "error_class": payload.get("error_class"),
                    "error": payload.get("error"),
                    # The hand-off. Without this a `background_task_dispatched`
                    # row renders as a bare event naming nothing, and the link
                    # from a turn to the job it started — the whole reason the
                    # dispatch event is written — is invisible.
                    "child_agent_run_id": payload.get("child_agent_run_id"),
                    "phase": payload.get("phase"),
                    "at": event.timestamp.isoformat() if event.timestamp else None,
                }
            )

    return inspector_surface_v09(
        runs=runs,
        selected=_run_dict(selected_row) if selected_row is not None else None,
        events=events,
        surface_id=surface_id,
    )


def _as_uuid(value: str) -> UUID:
    """A run id from a URL. Invalid input must not reach the query."""
    try:
        return UUID(value)
    except ValueError:
        # A nil uuid matches nothing, which is the right answer for "that is not
        # an id" — and it keeps the project scope in the WHERE clause rather
        # than short-circuiting around it.
        return UUID(int=0)


async def _grounding_messages(
    session: Any, project_id: UUID, claim_id: str | None, surface_id: str
) -> list[dict[str, Any]]:
    """Walk claim → citation → chunk → span → source, and bind the result.

    `claim_id` arrives under its own name now. It used to come through as
    `page_id` because `_parse_pane_specs` read exactly one hardcoded key, so
    every pane's parameter had to be called `page_id` whatever it actually was.

    Every hop is a real join. Nothing is synthesised: a claim with no citations,
    or citations with no resolvable chunks, renders as exactly that, because an
    ungrounded claim is the single most important thing this surface can tell an
    analyst.
    """
    from aleph_rks.models import DocumentChunk, Source
    from aleph_wiki.models import WikiClaim

    if not claim_id:
        return grounding_surface_v09(claim=None, groundings=[], surface_id=surface_id)
    try:
        cid = UUID(claim_id)
    except ValueError:
        return grounding_surface_v09(claim=None, groundings=[], surface_id=surface_id)

    claim_row = (
        await session.execute(
            select(WikiClaim).where(WikiClaim.id == cid, WikiClaim.project_id == project_id)
        )
    ).scalar_one_or_none()
    if claim_row is None:
        return grounding_surface_v09(claim=None, groundings=[], surface_id=surface_id)

    page_title = (
        await session.execute(select(WikiPage.title).where(WikiPage.id == claim_row.page_id))
    ).scalar_one_or_none()

    claim = {
        "id": str(claim_row.id),
        "text": claim_row.text,
        "confidence": claim_row.confidence,
        "page_id": str(claim_row.page_id),
        "page_title": page_title or "",
    }

    cites = list(
        (await session.execute(select(Citation).where(Citation.claim_id == cid))).scalars().all()
    )

    groundings: list[dict[str, Any]] = []
    for cite in cites:
        source_info: dict[str, Any] | None = None
        if cite.source_page_id is not None:
            sp = await session.get(SourcePage, cite.source_page_id)
            if sp is not None:
                src = await session.get(Source, sp.source_id)
                if src is not None:
                    source_info = {
                        "id": str(src.id),
                        "short_id": src.short_id,
                        "title": src.title,
                        "url": src.url,
                        "retracted": src.status == "retracted",
                    }

        chunk_ids = [UUID(x) for x in (cite.chunk_ids or []) if _is_uuid(x)]
        chunks: list[dict[str, Any]] = []
        if chunk_ids:
            rows = list(
                (
                    await session.execute(
                        select(DocumentChunk)
                        .where(DocumentChunk.id.in_(chunk_ids))
                        .order_by(DocumentChunk.ordinal)
                    )
                )
                .scalars()
                .all()
            )
            chunks = [
                {
                    "id": str(ch.id),
                    "ordinal": ch.ordinal,
                    "text": ch.text,
                    "char_start": ch.char_start,
                    "char_end": ch.char_end,
                    "section_path": ch.section_path,
                }
                for ch in rows
            ]

        groundings.append(
            {
                "marker": cite.citation_marker,
                "source": source_info,
                "chunks": chunks,
            }
        )

    return grounding_surface_v09(claim=claim, groundings=groundings, surface_id=surface_id)


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True

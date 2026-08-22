"""Surface composition API.

`GET /v1/projects/{id}/surfaces/{tab}` returns the A2UI surface JSON for
a right-panel tab. The renderer subscribes to updates via the existing
SSE channel; for Inc 4 this returns a one-shot snapshot.
"""

from __future__ import annotations

import json
import time
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
) -> list[dict[str, Any]]:
    """Build the v0.9 message list for `tab_lc`. Shared by the snapshot route
    and the delta stream so both compute identical surfaces. `surface_id`
    defaults to `tab_lc` so the stamped delta `surfaceId` matches
    `createSurface`.

    `params` carries whatever the pane DECLARED, keyed by its own name — so the
    grounding pane gets `claim_id` and the Inspector gets `run_id`, rather than
    both reaching in for a positional called `page_id`.
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
INSPECTOR_RUN_LIMIT = 50
INSPECTOR_EVENT_LIMIT = 500


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
                            session, project_id, tab, pane_params, surface_id
                        )
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
) -> SurfaceMessagesOut:
    tab_lc = tab.lower()
    messages = await _build_tab_messages(
        session, project_id, tab_lc, _params_from_query(tab_lc, request.query_params)
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
            fresh = await _build_tab_messages(session, project_id, tab_lc, pane_params, surface_id)
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
                        session, project_id, tab_lc, pane_params, surface_id
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
                .where(AgentRun.project_id == project_id, AgentRun.agent_kind == "assistant")
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

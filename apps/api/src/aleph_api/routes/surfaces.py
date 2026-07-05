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
    artifacts_surface_v09,
    briefs_surface_v09,
    hypotheses_surface_v09,
    notes_surface_v09,
    wiki_surface_v09,
)
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

router = APIRouter(prefix="/v1/projects", tags=["surfaces"])

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


async def _build_tab_messages(
    session: Any,
    project_id: UUID,
    tab_lc: str,
    page_id: str | None = None,
    surface_id: str | None = None,
) -> list[dict[str, Any]]:
    """Build the v0.9 message list for `tab_lc`. Shared by the snapshot route
    and the delta stream so both compute identical surfaces. `surface_id`
    defaults to `tab_lc` so the stamped delta `surfaceId` matches
    `createSurface`."""
    sid = surface_id or tab_lc
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
    msg = f"unknown tab: {tab_lc}"
    raise NotFound(msg)


@router.get("/{project_id}/surfaces/{tab}", response_model=None)
async def get_surface(
    project_id: ProjectScopeDep,
    tab: str,
    session: SessionDep,
    page_id: str | None = Query(default=None),
) -> SurfaceMessagesOut:
    tab_lc = tab.lower()
    messages = await _build_tab_messages(session, project_id, tab_lc, page_id)
    return SurfaceMessagesOut(tab=tab_lc, messages=messages)


@router.get("/{project_id}/surfaces/{tab}/stream", response_model=None)
async def stream_surface(
    project_id: Annotated[UUID, Path(...)],
    tab: str,
    request: Request,
    principal: PrincipalDep,
    page_id: str | None = Query(default=None),
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
    raw_cid = request.query_params.get("cid")
    cid = f"{project_id}:{tab_lc}:{raw_cid}" if raw_cid else None
    last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))

    async def _gen() -> AsyncIterator[bytes]:
        _sweep_buffers()
        buf = _STREAM_BUFFERS.get(cid) if cid else None

        async with maker() as session:
            fresh = await _build_tab_messages(session, project_id, tab_lc, page_id, surface_id)
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
                        session, project_id, tab_lc, page_id, surface_id
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
    return wiki_surface_v09(pages=pages, open_page=open_page, surface_id=surface_id)


async def _retracted_page_ids(session: Any, project_id: UUID, page_ids: list[UUID]) -> set[UUID]:
    """Pages (of ``page_ids``) carrying ≥1 retracted-confidence claim on their
    current revision. One query; drives the WP-6 `retracted` reader marker.

    A retraction (``aleph_reviewer.retraction.retract_source``) sets the
    dependent claims' ``confidence="retracted"``; a page is marked retracted iff
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
                WikiClaim.confidence == "retracted",
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
    source_page_ids = {c.source_page_id for c in cite_rows if c.source_page_id is not None}
    titles: dict[UUID, str] = {}
    urls: dict[UUID, str | None] = {}
    if source_page_ids:
        titles = dict(
            (
                await session.execute(
                    select(WikiPage.id, WikiPage.title).where(WikiPage.id.in_(source_page_ids))
                )
            ).all()
        )
        # source_page_id → SourcePage.source_id → Source.url (best-effort).
        sp_rows = list(
            (
                await session.execute(
                    select(SourcePage.page_id, Source.title, Source.url)
                    .join(Source, Source.id == SourcePage.source_id)
                    .where(SourcePage.page_id.in_(source_page_ids))
                )
            ).all()
        )
        for page_id_, src_title, src_url in sp_rows:
            urls[page_id_] = src_url
            titles.setdefault(page_id_, src_title)
    out: list[dict[str, Any]] = []
    for c in cite_rows:
        spid = c.source_page_id
        out.append(
            {
                "marker": c.citation_marker,
                "claim_id": str(c.claim_id),
                "source_page_id": str(spid) if spid is not None else None,
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

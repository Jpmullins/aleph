"""belief_rebuild_job: re-derive a project's belief graph from its sources.

`BeliefService.rebuild` has existed, tested, for as long as the belief layer
has, and until this file it had **no non-test caller**. Three separate comments
on the live write path — in `upsert_claim`, in `_node_claim_extraction`, and in
`rebuild`'s own docstring — name it as the remedy for a claim that was written
without a quote, a span, or a vector. `docs/decisions.md` D9 rests on it too:
the pre-existing thin citations stay *because they are re-derivable*, and
re-derivable means there is something that re-derives them.

The reason it had no caller was structural rather than an oversight, and it is
worth stating because it is the shape of defect this codebase keeps finding:
`Extractor` was typed as a **sync** callable while the only real extractor in
the tree, `aleph_wiki.claim_extraction.extract_claims`, is `async def`. The
interface excluded its own only implementation, so the repair could never be
handed the thing that does the deriving. The type admits both now.

What this job does NOT do: delete anything. `rebuild` upserts by `claim_key`
and dedupes evidence by locator, so running it twice over an unchanged corpus
leaves the same graph. An ungrounded legacy citation is superseded by a
grounded one for the same claim; it is never dropped, because an append-only
ledger is not edited to improve a number.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select

from aleph_core.ids import uuid7
from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.models.model_profile import ModelProfile
from aleph_db.models.project import Project
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability.tracing import current_trace_id, start_span
from aleph_rks.models import DocumentChunk, Source
from aleph_security.principal import Principal
from aleph_workers.gateway import gateways

_log = structlog.get_logger(__name__)

#: Chunks read per source. A cap, not a guess: `extract_claims` logs what it
#: skipped rather than reporting a truncated read as a complete one, because a
#: source half-read that claims to be fully read is how a knowledge base grows
#: confident gaps.
MAX_CHUNKS_PER_SOURCE = 60


async def _finalize(
    maker: Any,
    run_id: UUID,
    status: str,
    result_payload: dict[str, Any] | None = None,
    *,
    error_text: str | None = None,
) -> None:
    """Every exit path reports. A run left `running` is what the reaper exists
    to clean up, and needing the reaper is a bug, not a design."""
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            return
        run.status = status
        run.completed_at = utcnow()
        if result_payload is not None:
            run.result_payload = result_payload
        if error_text is not None:
            run.error_text = error_text[:4096]
        await session.commit()


async def belief_rebuild_job(
    ctx: dict[str, Any], project_id_str: str, max_sources: int | None = None
) -> dict[str, Any]:
    import json

    from aleph_models.client import ChatMessage
    from aleph_wiki.belief_service import (
        BeliefService,
        ClaimUpsert,
        SourceText,
        claim_embedder_for,
    )
    from aleph_wiki.claim_extraction import ChunkRef, extract_claims
    from aleph_wiki.models import SourcePage

    maker = ctx["session_maker"]
    pid = UUID(project_id_str)
    litellm = await gateways(ctx).litellm(pid)

    with start_span("worker.belief_rebuild", **{"aleph.project_id": project_id_str}):
        async with maker() as session:
            project = (
                await session.execute(select(Project).where(Project.id == pid))
            ).scalar_one_or_none()
            profile = (
                await session.execute(select(ModelProfile).where(ModelProfile.project_id == pid))
            ).scalar_one_or_none()
            if project is None or profile is None:
                return {"sources": 0, "reason": "no project or no model profile"}
            owner = project.created_by
            bindings = dict(profile.bindings_jsonb)

            # A source is rebuildable only if it has BOTH a wiki page to hang
            # claims on and chunks to quote from. Selecting on the join rather
            # than on `sources` alone is what keeps the count honest: a source
            # with no chunks yields no claims, and reporting it as "rebuilt"
            # would make a no-op look like a repair.
            rows = list(
                (
                    await session.execute(
                        select(Source.id, Source.title, SourcePage.page_id)
                        .join(SourcePage, SourcePage.source_id == Source.id)
                        .where(
                            Source.project_id == pid,
                            select(DocumentChunk.id)
                            .where(DocumentChunk.source_id == Source.id)
                            .exists(),
                        )
                        .order_by(Source.created_at)
                    )
                ).all()
            )

        if not rows:
            return {"sources": 0, "reason": "no source has both a page and chunks"}

        # `max_sources` bounds a live repair, and the bound is REPORTED.
        #
        # A rebuild is the most expensive thing this system does to a corpus:
        # one synthesis call per batch of chunks, per source, against whatever
        # the project binds SYNTHESIS to. Running it unbounded as the only
        # option means the first trial of a repair path is also the largest.
        # `sources_skipped` goes in the result payload and the ledger, because a
        # partial pass reported as a complete one is how a knowledge base ends
        # up with confident gaps — the same reason `extract_claims` logs its own
        # truncation rather than returning quietly.
        skipped = 0
        if max_sources is not None and len(rows) > max_sources:
            skipped = len(rows) - max_sources
            rows = rows[:max_sources]

        pages = {sid: page_id for sid, _title, page_id in rows}
        sources = [SourceText(source_id=sid, text="", title=title) for sid, title, _p in rows]

        # `text=""` is deliberate and not a stub. This extractor reads CHUNKS —
        # the same passages retrieval serves — because a quote has to be
        # verifiable against an indexed passage to be worth storing. Handing the
        # whole document text alongside would be a second copy nothing reads.

        run_id = uuid7()
        async with maker() as session:
            session.add(
                AgentRun(
                    id=run_id,
                    project_id=pid,
                    agent_kind="belief_rebuild",
                    correlation_id=f"belief-rebuild-{run_id.hex}",
                    status="running",
                    started_at=utcnow(),
                    input_payload={
                        "project_id": project_id_str,
                        "sources": len(sources),
                        "max_sources": max_sources,
                    },
                    created_by=owner,
                )
            )
            await session.commit()

        principal = Principal(
            user_id=owner,
            subject="agent",
            email="",
            actor_kind="aleph_agent",
            agent_run_id=run_id,
        )

        async def _call(*, system_prompt: str, user_payload: str, purpose: str) -> dict[str, Any]:
            resp = await litellm.chat(
                principal=principal,
                project_id=pid,
                agent_run_id=run_id,
                capability=Capability.SYNTHESIS,
                profile_bindings=bindings,
                messages=[
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(role="user", content=user_payload),
                ],
                response_format={"type": "json_object"},
                temperature=0.0,
                max_tokens=4096,
                purpose=purpose,
            )
            if not resp.choices:
                return {}
            content = resp.choices[0].message.content or ""
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                start, end = content.find("{"), content.rfind("}")
                if start < 0 or end <= start:
                    return {}
                try:
                    parsed = json.loads(content[start : end + 1])
                except json.JSONDecodeError:
                    return {}
            return parsed if isinstance(parsed, dict) else {}

        # Vectors accumulate here as each source is extracted, and `rebuild`
        # reads them through `.get`. This ordering is the whole reason it works:
        # `rebuild` awaits `extract(source)` and only then upserts that source's
        # drafts, so a claim's vector is present by the time it is written.
        # Embedding inside `upsert_claim` instead would be one gateway round
        # trip per claim.
        vectors: dict[str, list[float]] = {}
        read = 0

        async def _extract(source: SourceText) -> list[ClaimUpsert]:
            nonlocal read
            async with maker() as session:
                chunk_rows = list(
                    (
                        await session.execute(
                            select(
                                DocumentChunk.id,
                                DocumentChunk.text,
                                DocumentChunk.char_start,
                            )
                            .where(DocumentChunk.source_id == source.source_id)
                            .order_by(DocumentChunk.ordinal)
                        )
                    ).all()
                )
            if not chunk_rows:
                return []
            drafts = await extract_claims(
                [ChunkRef(chunk_id=c, text=t, char_start=s or 0) for c, t, s in chunk_rows],
                source_id=source.source_id,
                page_id=pages[source.source_id],
                call_json=_call,
                title=source.title,
                max_chunks=MAX_CHUNKS_PER_SOURCE,
                purpose="wiki.belief_rebuild.extract",
            )
            read += 1
            if drafts:
                embedder = await claim_embedder_for(
                    client=litellm,
                    principal=principal,
                    project_id=pid,
                    agent_run_id=run_id,
                    profile_bindings=bindings,
                    texts=[d.text for d in drafts],
                    purpose="wiki.belief_rebuild.claim_embed",
                )
                for draft in drafts:
                    vector = embedder(draft.text)
                    if vector is not None:
                        vectors[draft.text] = vector
            return list(drafts)

        try:
            async with maker() as session:
                result = await BeliefService(session).rebuild(
                    principal=principal,
                    ledger=LedgerWriter(session),
                    project_id=pid,
                    extract=_extract,
                    sources=sources,
                    embed=vectors.get,
                )
                await session.commit()
        except Exception as exc:
            await _finalize(maker, run_id, "failed", error_text=f"{type(exc).__name__}: {exc}")
            raise

        payload = {
            "sources_read": read,
            "sources_skipped": skipped,
            "claims_before": result.claims_before,
            "claims_after": result.claims_after,
            "new_claims": result.new_claims,
            "citations_written": result.citations_written,
            # Not an error count. A refused quote is the grounding check
            # WORKING: the model paraphrased, and the citation was refused
            # rather than stored unverifiable.
            "citations_rejected": result.citations_rejected,
            "user_claims_preserved": result.user_claims_preserved,
        }

        async with maker() as session:
            await LedgerWriter(session).append(
                project_id=pid,
                actor_id=owner,
                actor_kind="aleph_agent",
                action_kind="belief.rebuilt",
                target_id=None,
                target_kind="wiki_claims",
                payload=payload,
                trace_id=current_trace_id(),
            )
            await session.commit()

    await _finalize(maker, run_id, "succeeded", payload)
    _log.info("worker.belief_rebuild.done", project_id=project_id_str, **payload)
    return payload

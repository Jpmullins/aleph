"""Index one normalized document: chunk first, embed second.

The order is the whole point. Chunking needs no model; embedding does. Writing
the chunk rows only *after* the embed call returned meant one wrong model name
took down keyword search too — a capability that has no dependency on any model
at all. On the deployed stack that produced 75 ingested sources, 45 normalized
documents and **zero** searchable chunks, with 45 index jobs sitting in
``running`` and no error recorded anywhere.

So:

1. Chunk the markdown and insert the rows with ``embedding = NULL``. The
   ``text_tsv`` trigger fires, the lexical leg is live, and the source is
   findable by the words it was written in.
2. Then try to embed. If the embedder is unbound, misnamed, wrong-width or
   simply down, the index stays at ``lexical_only`` with a stated reason and the
   caller reports a *failed* run — degraded, visible, and still useful.
3. On success, fill the vectors in a second pass and mark the index
   ``embedded``.

Every step is idempotent: re-indexing the same document replaces its chunks, and
re-embedding only touches chunks whose vector is NULL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy import delete, select, update

from aleph_core.errors import ValidationFailed
from aleph_core.grounding import strip_nul
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_models.profile import resolve_binding
from aleph_rks.chunking import chunk_markdown
from aleph_rks.embedding import (
    embed_texts,
    embedding_dim_mismatch,
    is_known_embedding_model,
)
from aleph_rks.models import (
    EMBEDDING_DIM,
    DocumentChunk,
    NormalizedDocument,
    RetrievalIndexRecord,
    Source,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from aleph_models.client import LiteLLMClient
    from aleph_security.principal import Principal

_log = structlog.get_logger(__name__)


class AssetReader(Protocol):
    """The one method indexing needs from the asset store."""

    def get(self, uri: str) -> bytes: ...  # pragma: no cover - protocol


#: Reasons an index can be `lexical_only`. Each is a distinct operator action,
#: which is why they are separate strings rather than one boolean: "you have not
#: bound an embedder" and "the embedder you bound is down" are not the same
#: problem and were previously both reported as silence.
REASON_UNBOUND = "no embedding capability is bound on this project's model profile"
REASON_DIM = "the bound embedding model's output width does not fit the store"
REASON_UNAVAILABLE = "the embedding model did not answer"


@dataclass(frozen=True)
class IndexOutcome:
    """What indexing achieved, honestly.

    ``state`` is one of :data:`aleph_rks.models.INDEX_STATES`. ``ok`` is False
    whenever the dense leg is missing, so a caller marks its run failed and an
    operator sees a problem — even though the source is searchable.
    """

    chunk_count: int
    state: str
    embedder_model: str | None
    reason: str | None

    @property
    def ok(self) -> bool:
        return self.state == "embedded"


async def _write_chunks(
    session: AsyncSession,
    *,
    normalized: NormalizedDocument,
    markdown: str,
) -> int:
    """Replace this document's chunks. Returns the count written."""
    chunks = chunk_markdown(markdown)
    await session.execute(
        delete(DocumentChunk).where(DocumentChunk.normalized_document_id == normalized.id)
    )
    for c in chunks:
        session.add(
            DocumentChunk(
                id=uuid7(),
                project_id=normalized.project_id,
                source_id=normalized.source_id,
                normalized_document_id=normalized.id,
                ordinal=c.ordinal,
                text=c.text,
                text_tsv="",  # the trigger on document_chunks fills this
                embedding=None,  # filled in the second pass, if there is one
                section_path=c.section_path,
                char_start=c.char_start,
                char_end=c.char_end,
                token_count=c.token_count,
                embedder_model=None,
            )
        )
    return len(chunks)


async def _upsert_index_record(
    session: AsyncSession,
    *,
    project_id: UUID,
    source_id: UUID,
    chunk_count: int,
    state: str,
    embedder_model: str | None,
    degraded_reason: str | None,
    created_by: UUID,
) -> None:
    existing = (
        await session.execute(
            select(RetrievalIndexRecord).where(RetrievalIndexRecord.source_id == source_id)
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            RetrievalIndexRecord(
                id=uuid7(),
                project_id=project_id,
                source_id=source_id,
                embedder_model=embedder_model,
                chunk_count=chunk_count,
                state=state,
                degraded_reason=degraded_reason,
                indexed_at=utcnow(),
                created_by=created_by,
            )
        )
        return
    existing.embedder_model = embedder_model
    existing.chunk_count = chunk_count
    existing.state = state
    existing.degraded_reason = degraded_reason
    existing.indexed_at = utcnow()


async def index_normalized_document(
    *,
    maker: Callable[[], Any],
    normalized_id: UUID,
    asset_store: AssetReader,
    litellm: LiteLLMClient,
    principal: Principal,
    profile_bindings: dict[str, Any],
    agent_run_id: UUID | None,
    purpose: str = "rks.embed",
) -> IndexOutcome:
    """Chunk a normalized document, then embed it. Chunks survive either way.

    ``maker`` is a session factory rather than a session because the chunk write
    must be **committed** before the embed call. Sharing one transaction would
    roll the chunks back with the failed embed, which is the bug this function
    exists to remove.
    """
    async with maker() as session:
        normalized = (
            await session.execute(
                select(NormalizedDocument).where(NormalizedDocument.id == normalized_id)
            )
        ).scalar_one_or_none()
        if normalized is None:
            msg = f"normalized document {normalized_id} not found"
            raise RuntimeError(msg)
        project_id = normalized.project_id
        source_id = normalized.source_id
        markdown_uri = normalized.markdown_uri

    # `strip_nul`, not `defang`: this runs over markdown that is already stored,
    # and the whole point of a grounding span is that `markdown[start:end]`
    # slices the stored document. Removing characters here would shift every
    # offset after the first one. U+FFFD is one character standing in for one
    # character, so the offsets survive. Documents normalised after the ingest
    # boundary learned about NUL never reach this with one.
    markdown = strip_nul(asset_store.get(markdown_uri).decode("utf-8"))

    # ---- pass 1: chunks, committed on their own -----------------------------
    async with maker() as session:
        normalized = (
            await session.execute(
                select(NormalizedDocument).where(NormalizedDocument.id == normalized_id)
            )
        ).scalar_one()
        chunk_count = await _write_chunks(session, normalized=normalized, markdown=markdown)
        if chunk_count == 0:
            await _upsert_index_record(
                session,
                project_id=project_id,
                source_id=source_id,
                chunk_count=0,
                state="embedded",
                embedder_model=None,
                degraded_reason=None,
                created_by=principal.user_id,
            )
            await session.commit()
            return IndexOutcome(0, "embedded", None, None)
        await _upsert_index_record(
            session,
            project_id=project_id,
            source_id=source_id,
            chunk_count=chunk_count,
            state="lexical_only",
            embedder_model=None,
            degraded_reason="embedding pending",
            created_by=principal.user_id,
        )
        src = (
            await session.execute(select(Source).where(Source.id == source_id))
        ).scalar_one_or_none()
        if src is not None:
            # Searchable now, by keyword. Recording anything else here would make
            # a live degraded index indistinguishable from a dead one.
            src.status = "indexed"
        await session.commit()

    async def _degrade(state: str, reason: str) -> IndexOutcome:
        async with maker() as session:
            await _upsert_index_record(
                session,
                project_id=project_id,
                source_id=source_id,
                chunk_count=chunk_count,
                state=state,
                embedder_model=None,
                degraded_reason=reason,
                created_by=principal.user_id,
            )
            await session.commit()
        _log.warning(
            "rks.index.degraded",
            source_id=str(source_id),
            project_id=str(project_id),
            state=state,
            reason=reason,
            chunk_count=chunk_count,
        )
        return IndexOutcome(chunk_count, state, None, reason)

    # ---- pass 2: the dense leg, best effort --------------------------------
    try:
        embed_model = resolve_binding(profile_bindings, "embedding").model
    except ValidationFailed:
        return await _degrade("lexical_only", REASON_UNBOUND)

    mismatch = embedding_dim_mismatch(embed_model)
    if mismatch is not None:
        return await _degrade(
            "lexical_only",
            f"{REASON_DIM}: '{embed_model}' emits {mismatch}-dim vectors, "
            f"document_chunks.embedding is {EMBEDDING_DIM}-dim",
        )

    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(DocumentChunk.id, DocumentChunk.text)
                    .where(
                        DocumentChunk.normalized_document_id == normalized_id,
                        DocumentChunk.embedding.is_(None),
                    )
                    .order_by(DocumentChunk.ordinal)
                )
            ).all()
        )
    if not rows:
        # Every chunk already carries a vector — nothing to do, and saying so is
        # what makes a repair pass idempotent.
        async with maker() as session:
            await _upsert_index_record(
                session,
                project_id=project_id,
                source_id=source_id,
                chunk_count=chunk_count,
                state="embedded",
                embedder_model=embed_model,
                degraded_reason=None,
                created_by=principal.user_id,
            )
            await session.commit()
        return IndexOutcome(chunk_count, "embedded", embed_model, None)

    try:
        if not is_known_embedding_model(embed_model):
            # One trivial input learns the real width, so an unknown embedder
            # with the wrong width costs one token instead of a document.
            probe = await embed_texts(
                client=litellm,
                principal=principal,
                project_id=project_id,
                agent_run_id=agent_run_id,
                profile_bindings=profile_bindings,
                texts=["dimension probe"],
                purpose=f"{purpose}.probe",
            )
            probe_dim = len(probe.embeddings[0]) if probe.embeddings else EMBEDDING_DIM
            if probe_dim != EMBEDDING_DIM:
                return await _degrade(
                    "lexical_only",
                    f"{REASON_DIM}: '{embed_model}' emits {probe_dim}-dim vectors, "
                    f"document_chunks.embedding is {EMBEDDING_DIM}-dim",
                )
        result = await embed_texts(
            client=litellm,
            principal=principal,
            project_id=project_id,
            agent_run_id=agent_run_id,
            profile_bindings=profile_bindings,
            texts=[r.text for r in rows],
            purpose=purpose,
        )
    except Exception as exc:
        return await _degrade("lexical_only", f"{REASON_UNAVAILABLE}: {type(exc).__name__}: {exc}")

    if len(result.embeddings) != len(rows):
        return await _degrade(
            "lexical_only",
            f"{REASON_UNAVAILABLE}: asked for {len(rows)} vectors, got {len(result.embeddings)}",
        )

    async with maker() as session:
        for row, embedding in zip(rows, result.embeddings, strict=True):
            await session.execute(
                update(DocumentChunk)
                .where(DocumentChunk.id == row.id)
                .values(embedding=embedding, embedder_model=result.model)
            )
        await _upsert_index_record(
            session,
            project_id=project_id,
            source_id=source_id,
            chunk_count=chunk_count,
            state="embedded",
            embedder_model=result.model,
            degraded_reason=None,
            created_by=principal.user_id,
        )
        await session.commit()

    return IndexOutcome(chunk_count, "embedded", result.model, None)

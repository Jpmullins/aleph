"""The deep-research loop must read the sources it fetched.

`WS-RS7`. Aleph's flagship research feature searched for papers, downloaded
them, ingested them — and then composed the report without ever opening one.
`_node_compose` built the model's entire evidence context as::

    listing = "\n".join(f"c{i}: {s.title}" + (f" — {s.url}" if s.url else ""))

Titles and links. No source text reached the model at any point, so the prose
was written from the model's own recollection of the subject, and `[c3]` meant
"the third thing this run happened to download" rather than "this is what that
document says". Measured on the live stack before the change: 831 citations,
830 with no quote and no chunk anchor; seven succeeded research runs, seven
synthesis proposals, and **zero** citations on any synthesis page, because
`build_report` passed `claims=[]` and the commit had nothing to write.

This file runs the compose node against a REAL corpus in Postgres — chunks
written the way ingest writes them, with real character offsets into a real
document — and a fake gateway in process. What each test pins:

* **The composer sees source text.** The recorded `/v1/chat/completions` body
  carries the seeded passage verbatim. This is the criterion that fails against
  the old code no matter what else is true.
* **A marker resolves to an exact span.** Every citation the run produces names
  a chunk, carries a quote, and its `char_start`/`char_end` slice that exact
  quote out of the source document — not out of the chunk, out of the document.
* **A fabricated quote blocks the commit.** A quote present in no chunk fails
  the run before synthesis, and no `wiki_revisions` row is written.
* **Claims are no longer discarded**, so citation rows exist at all.

Two things this file does NOT prove, stated here so a green run is not read as
covering them. First, retrieval *quality*: the fixture is small and its
questions match its passages by construction, so this measures plumbing. The
quality number is `aleph_evals.retrieval_eval`. Second, the DB-level citation
anchor: `_node_commit_revision` still builds `CitationDraft(chunk_ids=[],
source_page_id=None, ...)` and `WikiService.commit_revision` writes no
`quote`/`chunk_id`/`char_start`/`char_end` column at all. Both files belong to
another workstream. The two tests that assert the anchor reaches Postgres WERE
`xfail(strict=True)`; the quote/chunk/span one now passes and its marker is
gone. `source_id` is still dropped, so that one stays marked — it will fail the
suite the moment somebody wires it,
which is the point.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, cast

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from aleph_core.grounding import ground
from aleph_core.ids import uuid7
from aleph_models.client import LiteLLMClient
from aleph_models.pricing import PricingTable
from aleph_models.testing import FakeGateway, GatewayConfig, RecordingSessions
from aleph_research.evidence import FabricatedQuote
from aleph_research.research_workflow import (
    IngestedSource,
    ResearchLimits,
    Subquery,
    _active_ctx_var,
    _Ctx,
    _node_compose,
    _node_synthesize,
)
from aleph_rks.models import DocumentChunk
from aleph_security.principal import Principal
from aleph_wiki.models import Citation, WikiClaim, WikiRevision

pytestmark = pytest.mark.integration

DEFAULT_URL = "postgresql+asyncpg://aleph:aleph@localhost:5432/aleph"
ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000c7")

#: Project-scoped teardown, in delete order. Named explicitly rather than
#: reflected: DATABASE_URL normally points at the running compose Postgres,
#: which holds a real corpus, so a truncate-everything teardown is a data-loss
#: bug waiting for its first run. `action_ledger_events` is deliberately absent
#: — it carries an append-only DELETE trigger and a fixture must not switch off
#: an invariant to tidy up.
_TEARDOWN_SQL = (
    "DELETE FROM agent_events WHERE agent_run_id IN"
    " (SELECT id FROM agent_runs WHERE project_id = :pid)",
    "DELETE FROM agent_runs WHERE project_id = :pid",
    "DELETE FROM citations WHERE project_id = :pid",
    "DELETE FROM claim_edges WHERE project_id = :pid",
    "DELETE FROM wiki_claims WHERE project_id = :pid",
    "DELETE FROM wiki_links WHERE project_id = :pid",
    "DELETE FROM synthesis_proposals WHERE project_id = :pid",
    "DELETE FROM wiki_index WHERE project_id = :pid",
    # `wiki_revisions` is deliberately absent for the same reason as
    # `action_ledger_events`: it carries an append-only DELETE trigger, and
    # `aleph` is a superuser here, so a teardown *could* bypass it with
    # `session_replication_role`. A fixture that switches off an invariant to
    # tidy up is how the invariant stops being one. The rows are scoped to a
    # throwaway project id and interfere with nothing.
    "DELETE FROM wiki_pages WHERE project_id = :pid",
    "DELETE FROM document_chunks WHERE project_id = :pid",
    "DELETE FROM normalized_documents WHERE project_id = :pid",
    "DELETE FROM sources WHERE project_id = :pid",
    "DELETE FROM model_calls WHERE project_id = :pid",
    "DELETE FROM cost_ledger_events WHERE project_id = :pid",
    "DELETE FROM projects WHERE id = :pid",
)


# ---------------------------------------------------------------------------
# The corpus. Two documents of real prose, chunked on paragraph boundaries the
# way `aleph_rks.chunking` does it, so `document[char_start:char_end]` is the
# chunk's text by construction — which is the property the span arithmetic in
# `evidence.anchor_body` depends on.
# ---------------------------------------------------------------------------

DOC_A = (
    "# Cryobiosis in tardigrades\n\n"
    "Tardigrades enter cryobiosis when ambient water freezes rather than "
    "evaporates. The transition is reversible and does not require the animal "
    "to lose its intracellular water.\n\n"
    "Trehalose accumulation is often described as the mechanism, but several "
    "eutardigrade species accumulate none and survive regardless. The "
    "trehalose hypothesis therefore cannot be general.\n\n"
    "Intrinsically disordered proteins, not sugars, appear to do the "
    "vitrification work in those species.\n"
)

DOC_B = (
    "# Radiation tolerance\n\n"
    "Desiccated tardigrades tolerate doses above one kilogray without a "
    "measurable drop in survival. Hydrated animals do not.\n\n"
    "The Dsup protein binds chromatin and shields it from hydroxyl radicals "
    "produced by ionising radiation.\n"
)

#: A sentence that appears in the corpus and nowhere in a title or a URL. If it
#: reaches the compose prompt, the composer was handed source text; the old
#: title listing could not have carried it under any circumstances.
DISTINCTIVE = (
    "Trehalose accumulation is often described as the mechanism, but several "
    "eutardigrade species accumulate none and survive regardless."
)

#: What the composer is told to copy. Both are verbatim spans of the corpus.
QUOTE_A = "The trehalose hypothesis therefore cannot be general."
QUOTE_B = "The Dsup protein binds chromatin and shields it from hydroxyl radicals"

TOPIC = "tardigrade cryobiosis and radiation tolerance"


def _paragraph_spans(document: str) -> list[tuple[int, int]]:
    """Character spans of each paragraph, offsets into `document`."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for block in document.split("\n\n"):
        start = document.index(block, cursor)
        spans.append((start, start + len(block)))
        cursor = start + len(block)
    return spans


@dataclass
class Corpus:
    project_id: uuid.UUID
    #: A real `agent_runs` row. `with_phase` writes progress events against it,
    #: and `_node_commit_revision` reads `state["agent_run_id"]` unconditionally
    #: — a research run without one raises `KeyError` at commit, which is a
    #: latent defect in a file this workstream does not own.
    agent_run_id: uuid.UUID
    source_a: uuid.UUID
    source_b: uuid.UUID
    short_a: str
    short_b: str
    documents: dict[uuid.UUID, str]

    def sources(self) -> list[IngestedSource]:
        return [
            IngestedSource(
                short_id=self.short_a,
                source_id=self.source_a,
                title="Cryobiosis in tardigrades",
                url="https://example.invalid/a",
                kind="upload",
            ),
            IngestedSource(
                short_id=self.short_b,
                source_id=self.source_b,
                title="Radiation tolerance",
                url=None,
                kind="upload",
            ),
        ]


# ---------------------------------------------------------------------------
# Fixtures. Local to this file: `tests/integration/conftest.py` is not visible
# from `tests/e2e/`, and this file must not depend on it.
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test — a session-scoped async engine binds its asyncpg
    pool to the first test's event loop and every later test then fails with
    "attached to a different loop"."""
    eng = create_async_engine(os.environ.get("DATABASE_URL", DEFAULT_URL), poolclass=None)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def maker(engine: AsyncEngine) -> Callable[[], AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def corpus(maker: Callable[[], AsyncSession]) -> AsyncIterator[Corpus]:
    """Two documents, chunked on paragraphs, committed and torn down."""
    project_id = uuid7()
    built = Corpus(
        project_id=project_id,
        agent_run_id=uuid7(),
        source_a=uuid7(),
        source_b=uuid7(),
        short_a=f"s{uuid.uuid4().hex[:8]}",
        short_b=f"s{uuid.uuid4().hex[:8]}",
        documents={},
    )
    built.documents = {built.source_a: DOC_A, built.source_b: DOC_B}
    try:
        async with maker() as session:
            await session.execute(
                text(
                    "INSERT INTO projects (id, title, model_profile_id, created_by)"
                    " VALUES (:id, :title, :mp, :actor)"
                ),
                {
                    "id": project_id,
                    "title": f"rs7 {project_id.hex[:8]}",
                    "mp": uuid7(),
                    "actor": ACTOR,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO agent_runs (id, project_id, agent_kind, correlation_id,"
                    " status, input_payload, created_by)"
                    " VALUES (:id, :pid, 'deep_research', :corr, 'running', '{}', :actor)"
                ),
                {
                    "id": built.agent_run_id,
                    "pid": project_id,
                    "corr": f"rs7-{project_id.hex[:12]}",
                    "actor": ACTOR,
                },
            )
            for source_id, short_id, title in (
                (built.source_a, built.short_a, "Cryobiosis in tardigrades"),
                (built.source_b, built.short_b, "Radiation tolerance"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO sources (id, project_id, connector_kind, title, short_id,"
                        " status, source_metadata_jsonb, created_by)"
                        " VALUES (:id, :pid, 'upload', :title, :short, 'normalized', '{}', :actor)"
                    ),
                    {
                        "id": source_id,
                        "pid": project_id,
                        "title": title,
                        "short": short_id,
                        "actor": ACTOR,
                    },
                )
                document = built.documents[source_id]
                normalized_id = uuid7()
                for ordinal, (start, end) in enumerate(_paragraph_spans(document)):
                    session.add(
                        DocumentChunk(
                            id=uuid7(),
                            project_id=project_id,
                            source_id=source_id,
                            normalized_document_id=normalized_id,
                            ordinal=ordinal,
                            text=document[start:end],
                            text_tsv="",  # the trigger on document_chunks fills this
                            embedding=None,  # lexical leg only; no embedder needed
                            section_path=None,
                            char_start=start,
                            char_end=end,
                            token_count=len(document[start:end].split()),
                            embedder_model=None,
                        )
                    )
            await session.commit()
        yield built
    finally:
        async with maker() as session:
            for statement in _TEARDOWN_SQL:
                await session.execute(text(statement), {"pid": project_id})
            await session.commit()


class _Scholar:
    """`style_pass` only — the research context's other scholar methods are not
    reached by compose or synthesize."""

    def style_pass(self, markdown: str) -> str:
        from aleph_scholar.style import style_pass

        return style_pass(markdown)


def _bindings() -> dict[str, Any]:
    """A profile bound to models the fake gateway actually serves.

    `titan-embed-text-v2` is the correct name; `titan-embed-v2` — one word out,
    the name Aleph shipped — is deliberately absent from `DEFAULT_MODELS`, and
    binding it here would reproduce the outage rather than test around it.
    """
    return {
        "synthesis": {"model": "claude-opus-4-7", "provider": "litellm"},
        "classification": {"model": "claude-haiku-4-5", "provider": "litellm"},
        "embedding": {"model": "titan-embed-text-v2", "provider": "litellm"},
    }


@dataclass
class Run:
    """What one compose attempt produced, whether or not it succeeded.

    `error` rather than a raised exception, because the prompt is evidence too:
    a run that refuses a fabricated quote still has to be inspectable for what
    the model was actually shown.
    """

    report: Any
    gateway: FakeGateway
    compose_prompt: str
    error: BaseException | None


async def _compose(
    corpus: Corpus,
    maker: Callable[[], AsyncSession],
    *,
    reply: str,
    then_synthesize: bool = False,
) -> Run:
    """Drive compose (and optionally synthesize) against the real corpus."""
    gateway = FakeGateway(GatewayConfig.well_behaved(chat_reply=reply))
    principal = Principal(
        user_id=ACTOR,
        subject="agent",
        email="",
        actor_kind="aleph_agent",
        agent_run_id=None,
        correlation_id="rs7",
    )
    report: Any = None
    error: BaseException | None = None
    async with gateway.client() as http:
        client = LiteLLMClient(
            base_url=gateway.base_url,
            api_key=gateway.api_key,
            http_client=http,
            pricing=PricingTable(),
            session_maker=cast("Any", RecordingSessions()),
        )
        ctx = _Ctx(
            session_maker=cast("Any", maker),
            litellm=client,
            principal=principal,
            scholar=cast("Any", _Scholar()),
            asset_store=cast("Any", None),
            tools_by_kind={},
            profile_bindings=_bindings(),
            # No barrier wait: the fixture committed the chunks already, so a
            # 90-second default would only be 90 seconds of nothing.
            limits=ResearchLimits(index_wait_seconds=0.0, index_poll_seconds=0.0),
            agent_token_secret="s",
            enqueue=_never_enqueued,
        )
        state: dict[str, Any] = {
            "agent_run_id": corpus.agent_run_id,
            "project_id": corpus.project_id,
            "topic": TOPIC,
            "ingested": corpus.sources(),
            "plan_subqueries": [
                Subquery(query="what mechanism explains tardigrade cryobiosis"),
                Subquery(query="how do tardigrades tolerate ionising radiation"),
            ],
        }
        token = _active_ctx_var.set(ctx)
        try:
            out = await _node_compose(cast("Any", state))
            report = out["report"]
            if then_synthesize:
                state["report"] = report
                await _node_synthesize(cast("Any", state))
        except Exception as exc:
            error = exc
        finally:
            _active_ctx_var.reset(token)

    chats = [r for r in gateway.requests if r.path == "/v1/chat/completions"]
    prompt = ""
    if chats and chats[-1].body:
        messages = cast("list[dict[str, Any]]", chats[-1].body.get("messages") or [])
        prompt = "\n".join(str(m.get("content") or "") for m in messages)
    return Run(report=report, gateway=gateway, compose_prompt=prompt, error=error)


async def _never_enqueued(*_args: Any, **_kwargs: Any) -> object:
    """`_node_synthesize` best-effort enqueues `curate_page_job`. There is no
    Redis here and curation is not what this file measures."""
    return None


_CARD_HEAD_RE = re.compile(r"^\[(c\d+)\] ", re.MULTILINE)


def _cards_from_prompt(prompt: str) -> dict[str, str]:
    """Marker → card body, parsed out of the prompt exactly as a model reads it.

    The tests build their quotes from THIS rather than from a hardcoded `c1`,
    because which chunk lands on which marker is a fact about the retriever, and
    a test that assumes it would break every time ranking changed — while
    proving nothing more.
    """
    cards: dict[str, str] = {}
    heads = list(_CARD_HEAD_RE.finditer(prompt))
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(prompt)
        stanza = prompt[head.end() : end]
        # First line is the header (short id · title · …); the body follows.
        _, _, body = stanza.partition("\n")
        cards[head.group(1)] = body.strip()
    return cards


def _first_sentence(text_block: str) -> str:
    """A quotable span: the card's first full sentence, verbatim."""
    stop = text_block.find(". ")
    if stop == -1:
        return text_block.strip().removesuffix(" […]").strip()
    return text_block[: stop + 1]


def _locate(corpus: Corpus, body: str) -> tuple[uuid.UUID, int] | None:
    """Which document a card came from, and at what character offset."""
    probe = body.removesuffix(" […]").strip()
    for source_id, document in corpus.documents.items():
        at = document.find(probe[:80])
        if at >= 0:
            return source_id, at
    return None


async def _grounded_reply(corpus: Corpus, maker: Callable[[], AsyncSession]) -> tuple[str, Run]:
    """A report that cites two real cards and quotes each of them verbatim.

    Built from a probe run so the quotes come from the pack the retriever
    actually produced. The probe cites nothing, so it fails with "anchored to
    nothing" — which is itself the shape every run of the OLD composer had.

    Markers are chosen from two different sources, preferring cards that start
    PAST the beginning of their document. That preference is load-bearing: a
    chunk at offset 0 makes a chunk-relative span and a document-relative span
    numerically identical, so a pack of first-paragraph cards cannot tell the
    two apart and `test_every_citation_resolves_to_a_verbatim_span` would pass
    against an implementation that forgot to add the chunk's own offset.
    """
    probe = await _compose(corpus, maker, reply="## Findings\n\nNothing is cited here.")
    cards = _cards_from_prompt(probe.compose_prompt)
    assert len(cards) >= 2, f"the pack must offer at least two cards, got {sorted(cards)}"
    located = {m: _locate(corpus, body) for m, body in cards.items()}
    ordered = sorted(
        cards,
        key=lambda m: (0 if (located[m] or (None, 0))[1] > 0 else 1, int(m[1:])),
    )
    markers: list[str] = []
    used_sources: set[uuid.UUID] = set()
    for marker in ordered:
        found = located[marker]
        if found is None or found[0] in used_sources:
            continue
        used_sources.add(found[0])
        markers.append(marker)
        if len(markers) == 2:
            break
    assert len(markers) == 2, f"need two cards from two sources, got {markers}"
    quotes = {m: _first_sentence(cards[m]) for m in markers}
    body = (
        "## Mechanism\n\n"
        f"The first source supports this reading of cryobiosis [{markers[0]}].\n\n"
        "## Radiation\n\n"
        f"The second source supports this reading of tolerance [{markers[1]}].\n\n"
    )
    block = ", ".join(f'"{m}": {json.dumps(q)}' for m, q in quotes.items())
    return body + '<!--aleph:evidence\n{"quotes": {' + block + "}}\n-->", probe


# ---------------------------------------------------------------------------
# Criterion 1 — the composer sees source text
# ---------------------------------------------------------------------------


async def test_compose_prompt_contains_source_text(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """FAILS against the old code by construction: its prompt was
    `cN: <title> — <url>` lines, and no title or URL contains this sentence."""
    reply, probe = await _grounded_reply(corpus, maker)
    assert DISTINCTIVE in probe.compose_prompt
    run = await _compose(corpus, maker, reply=reply)
    assert run.error is None, run.error
    assert DISTINCTIVE in run.compose_prompt


async def test_the_pack_is_chunks_not_whole_documents(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """A marker is a chunk. Handing the model both documents entire would put
    the source text in the prompt while making a citation mean nothing more
    precise than "somewhere in this paper"."""
    _, probe = await _grounded_reply(corpus, maker)
    cards = _cards_from_prompt(probe.compose_prompt)
    assert len(cards) >= 3
    assert all(len(body) < len(DOC_A) for body in cards.values())


async def test_compose_prompt_stays_inside_the_budget(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """The stated risk of this change is prompt size. It is bounded, and the
    bound is checked rather than asserted in a docstring."""
    from aleph_research.evidence import EVIDENCE_CHAR_BUDGET

    _, probe = await _grounded_reply(corpus, maker)
    body_chars = sum(len(b) for b in _cards_from_prompt(probe.compose_prompt).values())
    assert body_chars <= EVIDENCE_CHAR_BUDGET


async def test_the_prompt_names_the_sources_the_run_ingested(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """Retrieval is scoped to this run's own sources: a card carries the short
    id of a source this run downloaded, so the citation resolves."""
    _, probe = await _grounded_reply(corpus, maker)
    assert corpus.short_a in probe.compose_prompt
    assert corpus.short_b in probe.compose_prompt


# ---------------------------------------------------------------------------
# Criterion 2 — every citation resolves to a verbatim span
# ---------------------------------------------------------------------------


async def test_every_citation_resolves_to_a_verbatim_span(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """For every citation the run produces, the quote is really in the chunk —
    and the recorded span really is that quote's place in the DOCUMENT."""
    reply, _ = await _grounded_reply(corpus, maker)
    run = await _compose(corpus, maker, reply=reply)
    assert run.error is None, run.error
    refs = run.report.citations_by_marker
    assert refs, "a research report with no citations proves nothing"
    chunk_starts: list[int] = []
    async with maker() as session:
        for marker, ref in refs.items():
            chunk = (
                await session.execute(select(DocumentChunk).where(DocumentChunk.id == ref.chunk_id))
            ).scalar_one()
            assert ground(ref.quote, chunk.text) is not None, marker
            document = corpus.documents[chunk.source_id]
            assert document[ref.char_start : ref.char_end] == ref.quote, marker
            chunk_starts.append(chunk.char_start)
    # Without this the assertion above cannot fail on an implementation that
    # records the quote's offset INSIDE the chunk: for a chunk that begins at
    # document offset 0 the two numbers are identical.
    assert any(start > 0 for start in chunk_starts), (
        "every cited chunk began at document offset 0, so this test could not "
        "distinguish a document span from a chunk-relative one"
    )


async def test_a_citation_names_the_source_it_came_from(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    reply, _ = await _grounded_reply(corpus, maker)
    run = await _compose(corpus, maker, reply=reply)
    assert run.error is None, run.error
    shorts = {ref.source_short_id for ref in run.report.citations_by_marker.values()}
    assert shorts
    assert shorts <= {corpus.short_a, corpus.short_b}


# ---------------------------------------------------------------------------
# Criterion 3 — a fabricated quote blocks the commit
# ---------------------------------------------------------------------------


async def test_a_fabricated_quote_fails_the_commit(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """A quote present in no chunk raises, and nothing is committed.

    Dropping the marker instead would land a report whose prose still makes the
    assertion with the evidence quietly removed — which looks cited and is not.
    """
    reply = (
        "## Mechanism\n\nTardigrades were first cultured on Europa [c1].\n\n"
        "<!--aleph:evidence\n"
        '{"quotes": {"c1": "Tardigrades were first cultured on Europa in 1998."}}\n'
        "-->"
    )
    run = await _compose(corpus, maker, reply=reply, then_synthesize=True)
    assert isinstance(run.error, FabricatedQuote), run.error
    async with maker() as session:
        revisions = (
            await session.execute(
                select(WikiRevision).where(WikiRevision.project_id == corpus.project_id)
            )
        ).all()
    assert revisions == []


async def test_a_near_paraphrase_is_still_a_fabrication(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """The value of grounding is that its answer is not a judgement call. A
    quote one word off is not what the source says."""
    _, probe = await _grounded_reply(corpus, maker)
    cards = _cards_from_prompt(probe.compose_prompt)
    marker = sorted(cards, key=lambda m: int(m[1:]))[0]
    mangled = _first_sentence(cards[marker]).replace(" ", " probably ", 1)
    reply = (
        f"## Mechanism\n\nA statement about the mechanism [{marker}].\n\n"
        "<!--aleph:evidence\n"
        '{"quotes": {"' + marker + '": ' + json.dumps(mangled) + "}}\n-->"
    )
    run = await _compose(corpus, maker, reply=reply, then_synthesize=True)
    assert isinstance(run.error, FabricatedQuote), run.error


async def test_a_report_that_quotes_nothing_fails_the_commit(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """No evidence block at all: every marker is unanchored, so there is no
    report and no revision. This is the shape the old composer produced on
    every single run."""
    run = await _compose(
        corpus,
        maker,
        reply="## Mechanism\n\nTrehalose explains everything [c1].",
        then_synthesize=True,
    )
    assert isinstance(run.error, RuntimeError)
    assert "anchored to nothing" in str(run.error)
    async with maker() as session:
        revisions = (
            await session.execute(
                select(WikiRevision).where(WikiRevision.project_id == corpus.project_id)
            )
        ).all()
    assert revisions == []


# ---------------------------------------------------------------------------
# Criterion 5 — the report builder no longer discards claims
# ---------------------------------------------------------------------------


async def test_the_committed_page_carries_claims_and_citations(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """`build_report` passed `claims=[]`, so `_node_commit_revision` iterated an
    empty list and the research path wrote zero claims and zero citations. On
    the live stack: seven succeeded research runs, zero citations."""
    reply, _ = await _grounded_reply(corpus, maker)
    run = await _compose(corpus, maker, reply=reply, then_synthesize=True)
    assert run.error is None, run.error
    async with maker() as session:
        claims = (
            (
                await session.execute(
                    select(WikiClaim).where(WikiClaim.project_id == corpus.project_id)
                )
            )
            .scalars()
            .all()
        )
        citations = (
            (
                await session.execute(
                    select(Citation).where(Citation.project_id == corpus.project_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(claims) == 2, [c.text for c in claims]
    assert {c.section_anchor for c in claims} == {"mechanism", "radiation"}
    assert len(citations) == 2


# ---------------------------------------------------------------------------
# Criterion 4 — research-path citations carry a source anchor
#
# NOW TRUE, and these were `xfail(strict=True)` until it was.
#
# `_node_commit_revision` built `CitationDraft(chunk_ids=[], source_page_id=None,
# ...)` and `WikiService.commit_revision` wrote no `quote`, `chunk_id`,
# `char_start` or `char_end` column at all — so the run grounded every quote
# against its chunk and then discarded the result one function call later. The
# report already carried all four values on `ResearchSourceRef`; the commit path
# now reads them.
#
# `strict=True` is what made this land as a signal rather than a shrug: wiring
# it turned the suite RED with `XPASS(strict)`, which is a known defect
# announcing that it is fixed. A non-strict xfail would have gone quietly green
# and nobody would have removed the marker.
# ---------------------------------------------------------------------------


async def test_research_path_citations_carry_a_source_anchor(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    reply, _ = await _grounded_reply(corpus, maker)
    run = await _compose(corpus, maker, reply=reply, then_synthesize=True)
    assert run.error is None, run.error
    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(Citation).where(Citation.project_id == corpus.project_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows
    assert [r for r in rows if r.source_id is None] == []


async def test_research_path_citations_carry_a_quote_and_a_chunk(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    reply, _ = await _grounded_reply(corpus, maker)
    run = await _compose(corpus, maker, reply=reply, then_synthesize=True)
    assert run.error is None, run.error
    async with maker() as session:
        rows = (
            (
                await session.execute(
                    select(Citation).where(Citation.project_id == corpus.project_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows
    for row in rows:
        assert row.quote
        assert row.chunk_id is not None
        assert row.char_start is not None
        # `chunk_ids` as well as `chunk_id`. They are different columns and the
        # LIST is the wire format the grounding surface reads — a mutation
        # emptying it left every other assertion here green.
        assert row.chunk_ids, "chunk_ids is empty; the grounding surface reads that list"
        assert str(row.chunk_id) in row.chunk_ids
        # `verbatim` must be DERIVED from having a quote, not asserted. A row
        # flagged verbatim with no quote is a claim that something was checked
        # when nothing was.
        assert row.verbatim is True


async def test_a_citation_without_a_quote_is_not_flagged_verbatim(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """The negative half, and the reason `verbatim` is DERIVED rather than set.

    Two producers write citations and only one can supply a quote: the legacy
    stub/compile path cites a PAGE, not a passage. Hardcoding `verbatim=True`
    would mark those rows as evidence-checked — exactly the false confidence the
    column exists to prevent — and no test noticed, because every row in the
    research fixture has a quote. This commits one that does not.
    """
    from aleph_db.repos.ledger import LedgerWriter
    from aleph_wiki.models import Citation as _Citation
    from aleph_wiki.wiki_service import CitationDraft, ClaimDraft, WikiService

    async with maker() as session:
        await WikiService(session).commit_revision(
            principal=Principal(
                user_id=ACTOR,
                subject="agent",
                email="",
                actor_kind="aleph_agent",
                agent_run_id=None,
                correlation_id="rs7-verbatim",
            ),
            ledger=LedgerWriter(session),
            project_id=corpus.project_id,
            page_id=None,
            title="Unanchored citation probe",
            slug=None,
            page_kind="topic",
            body_md="# Probe\n\nA page whose citation names a source, not a passage.\n",
            summary="probe",
            claims=[
                ClaimDraft(
                    text="A claim cited to a page rather than a span.",
                    confidence="cited",
                    section_anchor=None,
                    citations=[
                        CitationDraft(chunk_ids=[], source_page_id=None, citation_marker="[c1]")
                    ],
                )
            ],
            wikilinks=[],
            commit_message="verbatim probe",
        )
        await session.commit()

    async with maker() as session:
        rows = list(
            (
                await session.execute(
                    select(_Citation).where(_Citation.project_id == corpus.project_id)
                )
            )
            .scalars()
            .all()
        )
    assert rows, "the probe committed no citation"
    for row in rows:
        assert row.quote is None
        assert row.verbatim is False, (
            "a citation with no quote is flagged verbatim — that asserts something "
            "was checked against the source when nothing was"
        )


# ---------------------------------------------------------------------------
# The index barrier — compose waits for what ingest enqueued
# ---------------------------------------------------------------------------


async def test_compose_waits_for_chunks_that_arrive_late(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """Ingest ENQUEUES normalize, which enqueues chunk_embed. At the moment the
    graph reaches compose the chunks of everything it just downloaded usually do
    not exist, and an evidence pack built a second too early is empty — which
    reads exactly like "these documents say nothing"."""
    from aleph_research import research_workflow as rw

    late = uuid7()
    missing = uuid7()
    polls: list[int] = []
    real_sleep = rw.asyncio.sleep

    async def _sleep_and_insert(_seconds: float) -> None:
        polls.append(len(polls))
        if len(polls) == 1:
            async with maker() as session:
                session.add(
                    DocumentChunk(
                        id=late,
                        project_id=corpus.project_id,
                        source_id=missing,
                        normalized_document_id=uuid7(),
                        ordinal=0,
                        text="A late-arriving passage about vitrification.",
                        text_tsv="",
                        embedding=None,
                        section_path=None,
                        char_start=0,
                        char_end=43,
                        token_count=6,
                        embedder_model=None,
                    )
                )
                await session.commit()
        await real_sleep(0)

    state = cast("Any", {"agent_run_id": None, "project_id": corpus.project_id, "topic": TOPIC})
    ctx = _Ctx(
        session_maker=cast("Any", maker),
        litellm=cast("Any", None),
        principal=cast("Any", None),
        scholar=cast("Any", None),
        asset_store=cast("Any", None),
        tools_by_kind={},
        profile_bindings={},
        limits=ResearchLimits(index_wait_seconds=5.0, index_poll_seconds=0.0),
        agent_token_secret="s",
        enqueue=_never_enqueued,
    )
    token = _active_ctx_var.set(ctx)
    try:
        rw.asyncio.sleep = _sleep_and_insert  # type: ignore[assignment]
        counts = await rw._await_chunks(state, {corpus.source_a, missing})
    finally:
        rw.asyncio.sleep = real_sleep  # type: ignore[assignment]
        _active_ctx_var.reset(token)
        async with maker() as session:
            await session.execute(
                text("DELETE FROM document_chunks WHERE id = :cid"), {"cid": late}
            )
            await session.commit()

    # It waited: `missing` had no chunk on the first look and one on the second.
    assert polls, "the barrier never polled — it would race every real research run"
    assert counts.get(missing) == 1
    assert corpus.source_a in counts


async def test_the_barrier_gives_up_rather_than_blocking_forever(
    corpus: Corpus, maker: Callable[[], AsyncSession]
) -> None:
    """A source whose normalize died must not hold the run open. The deadline is
    the bound; the log names what never indexed."""
    from aleph_research import research_workflow as rw

    never = uuid7()
    state = cast("Any", {"agent_run_id": None, "project_id": corpus.project_id, "topic": TOPIC})
    ctx = _Ctx(
        session_maker=cast("Any", maker),
        litellm=cast("Any", None),
        principal=cast("Any", None),
        scholar=cast("Any", None),
        asset_store=cast("Any", None),
        tools_by_kind={},
        profile_bindings={},
        limits=ResearchLimits(index_wait_seconds=0.0, index_poll_seconds=0.0),
        agent_token_secret="s",
        enqueue=_never_enqueued,
    )
    token = _active_ctx_var.set(ctx)
    try:
        counts = await rw._await_chunks(state, {corpus.source_a, never})
    finally:
        _active_ctx_var.reset(token)
    assert never not in counts
    assert corpus.source_a in counts

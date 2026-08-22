"""The second pass over retrieved chunks — WS-RS6.

Fusion answers "which passages did either ranking like". It cannot answer
"which of these actually answers the question", because RRF is a function of
*rank* alone: the top hit scores the same whether it is a perfect match or the
least-bad of a bad set. That is the gap a reranker closes, and it is why
`Capability.RERANK` existed as an enum member, a discovery policy and a row in
the Settings drawer for months with nothing behind it — a consumer with no
producer, this codebase's signature defect inverted.

**Two backends, and the second one is the one that runs here.**

* :class:`CrossEncoderReranker` posts to ``/v1/rerank``. That is the fast,
  cheap, deterministic path and it is what a gateway serving a Cohere-style
  reranker should be given.
* :class:`ListwiseLlmReranker` asks a chat model to judge the passages. Slower,
  non-deterministic, and measured — because **the deployed gateway serves no
  reranker at all**. All 26 models the key can reach are chat or embedding
  models; ``POST /v1/rerank`` is routed (it answers 400 "Invalid model name",
  not 404) and rejects every one of them. Shipping only the cross-encoder path
  would have been a feature that cannot run on the deployment it was written
  for.
* :class:`AdaptiveReranker` tries the first and remembers the answer. One
  wasted request per process per model, never one per search.

**Nothing is ever skipped in silence.** A search with no reranker still stamps
``retrieval.rerank.skipped`` on its span with the reason in words, because "the
reranker did nothing" and "no reranker is bound" are different problems and
both used to look like an ordinary result list.

**What it is worth.** Measured on 738 documents from this instance's own
corpus — 4,245 production-chunked passages, 236 answerable questions and 8
unanswerable ones, one seeded index, the LLM backend on `claude-haiku-4-5`:

|                       | nDCG@10 | MRR   | r@1  | r@3  | r@20 | abstain |
|-----------------------|---------|-------|------|------|------|---------|
| fusion only           | 0.567   | 0.495 | 0.38 | 0.55 | 0.84 | 0/8     |
| + LLM rerank          | 0.645   | 0.572 | 0.44 | 0.67 | 0.87 | **8/8** |
| + rerank, `keep_unranked=False` | 0.621 | 0.561 | 0.44 | 0.65 | 0.80 | 8/8 |
| a stub that REVERSES the ranking | 0.042 | 0.037 | 0.01 | 0.02 | 0.26 | 0/8 |

The last row is the one that matters most. A reranker whose output is computed
and then dropped is indistinguishable from one that agreed with fusion, so the
proof that it is consumed is that inverting its judgement collapses the number
— 0.567 to 0.042. `packages/aleph-rks/tests/test_rerank.py` pins the same
property without a gateway.

Abstention is the second result and it is a capability, not a delta: the
unanswerable questions went from 0 of 8 declined to 8 of 8. Nothing in Aleph
could do that before — a cosine-distance floor cannot separate answerable from
unanswerable queries because the distributions overlap (`docs/decisions.md`
D10). The cost is one answerable question in 236 wrongly declined.

The price is one model call per search: ~1.4s at the measured concurrency, and
the whole candidate window in the prompt. That is why `search_corpus` takes a
reranker rather than building one — a caller on the in-process agent path pays
that latency directly.

**The reranker spent a day judging and being ignored.** Bound to
`gemma-4-e2b` — the model this deployment actually binds to
`Capability.RERANK` — the 45-question committed set logged
`rks.rerank.unparseable` on **40 of 45 queries** and kept fusion order every
time. The reply was `{"relevant": [{"id": [4], "score": 2}]}`: the model had
copied the prompt's own `[4]` passage label into the id, so every entry was
dropped and the list read as "all 1 entries were unusable". Two changes, and
each fixes a different half:

* the prompt now says the id is a bare integer and asks for EVERY passage
  scored 1 or above, not just the best one — the same three probe questions
  went from one entry each to four, two and six against the live gateway;
* :func:`_passage_id` reads a one-element list as the passage it names, so the
  next model that echoes the brackets costs nothing.

Measured on the committed set, `--rerank both`, before and after:

|                | nDCG@10 | MRR   | r@1  | unparseable |
|----------------|---------|-------|------|-------------|
| fusion only    | 0.970   | 0.960 | 0.93 | —           |
| rerank, before | 0.978   | 0.971 | 0.96 | 40/45       |
| rerank, after  | **1.000** | **1.000** | **1.00** | **0/45** |

That set is saturated — 0.970 is a ceiling of 1.000 away, so `WS-RS6`'s
"+0.05 nDCG@10" cannot be shown here at all and needs the RS5 generated set.
What it does show is that the judgement now reaches the results: the arm is
perfect, and it was not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import UUID

import structlog

from aleph_core.errors import ValidationFailed
from aleph_core.schemas.model_profile import Capability
from aleph_models.client import ChatMessage, LiteLLMClient, RerankUnsupported
from aleph_models.profile import resolve_binding
from aleph_rks.retrieval import DEFAULT_RERANK_WINDOW as _DEFAULT_RERANK_WINDOW
from aleph_rks.retrieval import ChunkHit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aleph_security.principal import Principal

_log = structlog.get_logger(__name__)


#: How many fused candidates the reranker is shown. Re-exported from
#: :mod:`aleph_rks.retrieval`, which cannot import this module (it is imported
#: BY it, for `ChunkHit`).
#:
#: Fifty is what the plan asked for and it is also roughly where the LLM
#: backend stops being cheap: the whole candidate list goes into one prompt, so
#: the window multiplies the cost of every search. Forty keeps a full window
#: under ~12k prompt tokens at :data:`RERANK_SNIPPET_CHARS`.
DEFAULT_RERANK_WINDOW = _DEFAULT_RERANK_WINDOW

#: Characters of each chunk the LLM backend is shown.
#:
#: Chunks run to ~2,000 characters and the window is forty of them, so the
#: whole candidate set would be ~20k tokens per search. The judgement is made
#: on the snippet and applied to the whole chunk — that is a real
#: approximation, stated here rather than hidden, and it is why the snippet is
#: taken from the START of the chunk (where a passage's topic sentence lives)
#: rather than the middle.
RERANK_SNIPPET_CHARS = 1_200

#: Relevance floor. A passage the model scores below this is not "listed".
MIN_RELEVANT_SCORE = 1.0

_SYSTEM = (
    "You are a retrieval reranker. You judge whether a passage answers a "
    "question. You never write prose and you never invent passage ids."
)

_USER = """\
Question:
{query}

Passages:
{passages}

Score every passage that is relevant to the question:

  3 - answers the question directly
  2 - substantially about the question, part of an answer
  1 - mentions the subject; weak but not useless
  0 - unrelated. DO NOT list these.

Return JSON and nothing else:

{{"relevant": [{{"id": <passage id>, "score": <1, 2 or 3>}}, ...]}}

`id` is the BARE INTEGER that labels the passage: for a passage written
`[7] ...` write `"id": 7` — never `"id": [7]`, never `"id": "7"`.

Order the list most relevant first, and list EVERY passage you scored 1 or
above, not only the best one.

If NO passage is relevant to the question, return {{"relevant": []}} — an empty
list is the correct answer for a question this collection does not answer, and
guessing is worse than saying nothing.\
"""


class Reranker(Protocol):
    """A second pass over fused candidates.

    ``skipped_reason`` is part of the protocol rather than an absence, so
    ``search_corpus`` can stamp a *reason* on its span without knowing which
    implementation it is holding — and so "no reranker" has to say why it is
    not there. See :class:`NoReranker`.
    """

    #: A short, stable identifier for logs and spans (`llm-listwise`, ...).
    name: str
    #: ``None`` for a reranker that ranks. A sentence for one that does not.
    skipped_reason: str | None

    async def rank(self, *, query: str, hits: Sequence[ChunkHit], top_k: int) -> list[ChunkHit]: ...


@dataclass
class NoReranker:
    """The honest null object: returns the fused order and says why.

    A `None` reranker would make "reranking is off because nobody configured
    it" indistinguishable from "reranking ran and changed nothing", which is
    exactly the class of silence this repository keeps shipping.
    """

    skipped_reason: str | None
    name: str = "none"

    async def rank(self, *, query: str, hits: Sequence[ChunkHit], top_k: int) -> list[ChunkHit]:
        del query
        return list(hits[:top_k])


def _with_rerank_score(hit: ChunkHit, score: float | None, position: int) -> ChunkHit:
    """A copy carrying the reranker's judgement and its new position.

    Kept rather than discarded on purpose: `cosine_distance` and `lexical_rank`
    were both computed and thrown away by the legs that produced them, and the
    absolute signal that could tell a real match from the nearest irrelevant
    passage went with them. A rerank score is the strongest such signal Aleph
    has ever had; dropping it here would repeat the defect on the same file.
    """
    return ChunkHit(
        chunk_id=hit.chunk_id,
        ordinal=hit.ordinal,
        text=hit.text,
        section_path=hit.section_path,
        # The fused score is left exactly as fusion computed it. Overwriting it
        # with the rerank score would make two incomparable scales share one
        # field, and every existing reader of `.score` would silently change
        # meaning.
        score=hit.score,
        source_id=hit.source_id,
        cosine_distance=hit.cosine_distance,
        lexical_rank=hit.lexical_rank,
        rerank_score=score,
        rerank_position=position,
    )


def _loads_lenient(content: str) -> dict[str, Any]:
    """Parse JSON a model wrapped in a fence or a sentence.

    ``response_format={"type": "json_object"}`` is a request the gateway
    forwards, not a guarantee the model honours — measured on this deployment's
    own models, a strict `json.loads` failed on the large majority of replies,
    every one of which contained valid JSON inside a ```json fence. Failing
    there would look like a reranker that judged nothing relevant, i.e. like an
    abstention, which is the worst possible way for a parse error to present.
    """
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text[3:]
        if text.lstrip().startswith("json"):
            text = text.lstrip()[4:]
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            msg = f"no JSON object in reranker reply: {content[:160]!r}"
            raise ValueError(msg) from None
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        msg = f"reranker reply is not a JSON object: {content[:160]!r}"
        raise ValueError(msg)
    return cast_dict(parsed)


def cast_dict(value: dict[Any, Any]) -> dict[str, Any]:
    """Narrow a parsed JSON object for the type checker."""
    return {str(k): v for k, v in value.items()}


@dataclass(frozen=True)
class Judgement:
    """What the model said, and whether it said anything intelligible.

    The two states this separates were one value until 2026-08-22, and
    collapsing them is destructive rather than merely lossy:

    * **A genuine abstention** — the model returned a well-formed, empty
      `relevant` list. That is the signal that empties the result, and it is
      the only thing in Aleph that can tell an answerable question from an
      unanswerable one (docs/decisions.md D10).
    * **An unintelligible reply** — no `relevant` key, or one that is not a
      list, or a list from which every single entry had to be dropped. The
      model has told us nothing, and reading that as "none of these passages
      are relevant" is a broken reranker reporting perfect humility.

    Measured: with `gemma-4-e2b` bound to `Capability.RERANK`, treating the
    second as the first took the 45-question eval from **nDCG@10 0.970 to
    0.133** — recall@20 fell from 1.00 to 0.13. Retrieval was not returning
    worse answers; it was returning nothing, and reporting an abstention rate.
    """

    scores: list[tuple[int, float]]
    #: `None` when the reply was intelligible. A sentence otherwise.
    malformed: str | None = None
    #: The model judged real candidates and scored EVERY one of them below the
    #: relevance floor. A third state, added 2026-08-22, and it is the one that
    #: was making abstention impossible in practice.
    #:
    #: `{"relevant": []}` is the abstention the prompt asks for. What the bound
    #: model actually does is answer `{"relevant": [{"id": 3, "score": 0}]}` —
    #: it names a real passage, it understood the scale, and it says the
    #: passage is unrelated. That is a judgement, and it was landing in
    #: `malformed` beside genuine notation failures and being discarded.
    #: Measured on 8 off-corpus questions with `gemma-4-e2b`: 6 of them were
    #: declined by the model and thrown away, which is why the abstain rate was
    #: 2/8 rather than 8/8 with the reranker doing exactly what it should.
    declined: bool = False


def _passage_id(value: object) -> int | None:
    """The passage index a model meant, or None when it is not decidable.

    Only one shape is normalised, and it is a NOTATION echo rather than a
    guess: a one-element list, ``"id": [7]``, for a passage the prompt labels
    ``[7]``. The model has copied the label including its brackets, and there
    is exactly one integer inside it, so which passage it names is not in
    doubt.

    Measured on this deployment: with `gemma-4-e2b` bound to
    `Capability.RERANK`, **40 of the 45 eval queries** came back as
    ``{"relevant": [{"id": [4], "score": 2}]}`` and every one was dropped —
    `rks.rerank.unparseable`, "all 1 entries were unusable", fused order kept.
    The reranker was making a judgement on 89% of searches and throwing it
    away. The prompt now says the id is a bare integer (which fixes the model
    that was measured), and this fixes the next model that does it anyway.

    Everything else still goes out the door dropped, because everything else
    IS a guess: ``[4, 7]`` names two passages and repairing it would invent an
    intent, and ``"4"`` as a string is cheap to accept but is the shape a model
    also uses for a passage LABEL it invented, so it earns no exception.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, list):
        inner = cast("list[object]", value)
        if len(inner) == 1 and isinstance(inner[0], int) and not isinstance(inner[0], bool):
            return inner[0]
    return None


def _relevance_score(row: dict[str, Any]) -> float | None:
    """The score the model gave a listed passage, or None when unreadable.

    **A missing `score` key is not unreadable.** The prompt asks the model to
    "list EVERY passage you scored 1 or above", so appearing in `relevant` is
    itself the judgement — the number only refines the ORDER within a set the
    model has already decided is relevant. Measured on this deployment with
    `gemma-4-e2b`: of 6 replies discarded as unusable in one 48-question run,
    4 were `{"relevant": [{"id": 1}, {"id": 2}]}` — two real passage ids, no
    scores, thrown away whole. That is the same class as the `{"id": [4]}`
    notation echo `_passage_id` already handles: the model answered, in a
    shape the parser did not accept.

    Defaulting to the floor rather than to the top of the scale is the
    conservative reading: it asserts "relevant", which the list membership
    already says, and nothing more. Ties then fall to `apply_ranking`'s
    secondary key, the fused candidate index — so a reply with no scores at all
    is read as a SET of relevant passages in fusion order, which is exactly
    what the model gave us.

    A score that is PRESENT and unreadable — `"score": "high"`, `"score": true`
    — is still dropped. There the model tried to say something and we cannot
    tell what, and inventing a value would be a guess wearing its authority.
    """
    if "score" not in row:
        return MIN_RELEVANT_SCORE
    score = row["score"]
    if isinstance(score, bool) or not isinstance(score, int | float):
        return None
    return float(score)


def parse_judgement(reply: dict[str, Any], candidate_count: int) -> Judgement:
    """`(index, score)` pairs the model actually returned, cleaned — and why not.

    This is where an LLM reranker breaks: an id that names no candidate, the
    same id twice, a score outside the scale, a string where a number belongs.
    Each of those, left alone, silently reorders a passage the model never
    judged. Dropped rather than repaired — a repaired id is a guess wearing the
    model's authority. The single exception is :func:`_passage_id`'s one-element
    list, which is a notation echo and not a guess; read its docstring for what
    that cost before it was handled.

    Dropping every entry is different from being handed none, and dropping
    every entry FOR A REASON THE MODEL CHOSE is different again, so the three
    leave by different doors.

    The middle door is the one that was missing. The previous version said
    "distinguishing further would need per-entry reasons"; it needs one
    counter, and without it the reranker could not abstain on the deployment
    it runs on. `MIN_RELEVANT_SCORE` filtering is not a parse failure — a model
    that writes `{"id": 3, "score": 0}` has read the passage, understood the
    scale, and said no. Lumping that in with `{"id": [4]}` threw away the only
    abstention signal Aleph has (`docs/decisions.md` D10) 6 times out of 8.
    """
    raw = reply.get("relevant")
    if not isinstance(raw, list):
        present = ", ".join(sorted(str(k) for k in reply)) or "nothing"
        return Judgement(
            [],
            f"no 'relevant' list in the reply (keys: {present})",
        )
    seen: set[int] = set()
    out: list[tuple[int, float]] = []
    #: Entries the model wrote that we could not READ. Any of these means the
    #: reply is not trustworthy as a judgement.
    unreadable = 0
    #: Entries we read perfectly well and that say "not relevant".
    below_floor = 0
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            unreadable += 1
            continue
        row = cast_dict(cast("dict[Any, Any]", item))
        index = _passage_id(row.get("id"))
        score = _relevance_score(row)
        if index is None:
            unreadable += 1
            continue
        if score is None:
            unreadable += 1
            continue
        if not 0 <= index < candidate_count or index in seen:
            unreadable += 1
            continue
        if float(score) < MIN_RELEVANT_SCORE:
            below_floor += 1
            continue
        seen.add(index)
        out.append((index, float(score)))

    if not out and raw:
        if unreadable == 0:
            # Every entry named a real, distinct candidate and scored it below
            # the floor. The model answered the question it was asked. Its
            # answer is "none of these", and `apply_ranking` turns an empty
            # score list into an empty result — the abstention.
            #
            # `unreadable == 0`, not "mostly readable". One notation failure in
            # the list means we do not know what the model meant about that
            # passage, and a partial abstention is not one: the safe reading of
            # an unknown is that something in the list might have been
            # relevant, so fused order stands.
            return Judgement([], declined=True)
        # A non-empty list from which nothing survived, and at least one entry
        # was unreadable. The model may or may not have judged; we cannot tell,
        # and reading it as "nothing is relevant" is a broken reranker
        # reporting perfect humility.
        return Judgement(
            [],
            f"all {len(raw)} entries were unusable — {unreadable} unreadable "
            f"(bad id, bad score, duplicate or out of range), {below_floor} "
            "scored below the relevance floor",
        )
    return Judgement(out)


def parse_scores(reply: dict[str, Any], candidate_count: int) -> list[tuple[int, float]]:
    """`parse_judgement`, discarding the reason. Kept for callers that only
    need the pairs."""
    return parse_judgement(reply, candidate_count).scores


def apply_ranking(
    hits: Sequence[ChunkHit],
    scored: list[tuple[int, float]],
    *,
    top_k: int,
    keep_unranked: bool,
) -> list[ChunkHit]:
    """Reorder `hits` by the model's judgement. Pure, so it can be tested alone.

    Two rules, and both are decisions rather than details:

    * **An empty judgement empties the result.** The model saying "none of
      these is relevant" is the abstention signal, and passing the fused list
      through anyway would throw away the only thing in Aleph that can tell an
      answerable question from an unanswerable one — a cosine floor cannot
      (docs/decisions.md D10).
    * **A partial judgement does not lose recall.** When the model listed
      *something*, unlisted candidates keep their fused order BEHIND the
      judged ones instead of being deleted. A stingy reranker then costs
      ranking quality at worst, never a hit that fusion had already found.
      ``keep_unranked=False`` is the strict variant, and it is a real quality
      trade rather than a cleanup: measure it before choosing it.
    """
    if not scored:
        return []
    ordered = sorted(scored, key=lambda pair: (-pair[1], pair[0]))
    taken = {index for index, _ in ordered}
    out = [
        _with_rerank_score(hits[index], score, position)
        for position, (index, score) in enumerate(ordered)
    ]
    if keep_unranked:
        out.extend(
            _with_rerank_score(hit, None, len(ordered) + offset)
            for offset, hit in enumerate(h for i, h in enumerate(hits) if i not in taken)
        )
    return out[:top_k]


@dataclass
class CrossEncoderReranker:
    """``POST /v1/rerank`` — the fast path, for a gateway that serves a reranker.

    Untested against a live reranker, and that is stated rather than implied:
    the deployment this was written on serves none, so :class:`AdaptiveReranker`
    exists to notice that at runtime instead of failing every search.
    """

    client: LiteLLMClient
    principal: Principal
    project_id: UUID
    profile_bindings: dict[str, Any]
    agent_run_id: UUID | None = None
    purpose: str = "rks.rerank"
    keep_unranked: bool = True
    name: str = "cross-encoder"
    skipped_reason: str | None = None

    async def rank(self, *, query: str, hits: Sequence[ChunkHit], top_k: int) -> list[ChunkHit]:
        if not hits:
            return []
        response = await self.client.rerank(
            principal=self.principal,
            project_id=self.project_id,
            agent_run_id=self.agent_run_id,
            profile_bindings=self.profile_bindings,
            query=query,
            documents=[h.text[:RERANK_SNIPPET_CHARS] for h in hits],
            top_n=min(top_k, len(hits)),
            purpose=self.purpose,
        )
        scored = [
            (r.index, r.relevance_score)
            for r in response.results
            if 0 <= r.index < len(hits) and r.relevance_score >= 0.0
        ]
        # A cross-encoder scores everything it is given, so an empty `results`
        # means the gateway returned nothing — not that nothing is relevant.
        # Treating it as an abstention would turn a transport oddity into
        # "I don't know", so the fused order stands.
        if not scored:
            _log.warning(
                "rks.rerank.empty_results",
                backend=self.name,
                candidates=len(hits),
                impact="fused order kept; this is NOT read as an abstention",
            )
            return list(hits[:top_k])
        return apply_ranking(hits, scored, top_k=top_k, keep_unranked=self.keep_unranked)


@dataclass
class ListwiseLlmReranker:
    """A chat model judging the candidate list in one call.

    The backend that actually runs on this deployment. It costs one LLM call
    per search — measured at roughly 9k prompt tokens for a forty-candidate
    window — and it is non-deterministic, which is a reproducibility cost the
    cross-encoder does not have. Both are the reason `WS-RS6`'s Iterate step
    asks whether it earns its place on the request path.
    """

    client: LiteLLMClient
    principal: Principal
    project_id: UUID
    profile_bindings: dict[str, Any]
    agent_run_id: UUID | None = None
    purpose: str = "rks.rerank.llm"
    keep_unranked: bool = True
    #: Zero, not the client default of 0.7. A reranker is a judgement, and a
    #: judgement that changes between two identical searches cannot be
    #: measured — nor explained to whoever asks why the answer moved.
    temperature: float = 0.0
    name: str = "llm-listwise"
    skipped_reason: str | None = None

    async def rank(self, *, query: str, hits: Sequence[ChunkHit], top_k: int) -> list[ChunkHit]:
        # Cleared per call. `skipped_reason` describes THIS search, and
        # `_search_and_rerank` reads it BEFORE calling `rank` and returns fused
        # order without reranking when it is set — so a value left over from a
        # previous unreadable reply does not merely mislabel one trace, it
        # switches the reranker off for the rest of the process. Measured on
        # the 208-question set: 8 replies were unreadable, and the first of them
        # would have disabled the other 200.
        self.skipped_reason = None
        if not hits:
            return []
        passages = "\n\n".join(f"[{i}] {h.text[:RERANK_SNIPPET_CHARS]}" for i, h in enumerate(hits))
        response = await self.client.chat(
            principal=self.principal,
            project_id=self.project_id,
            agent_run_id=self.agent_run_id,
            capability=Capability.RERANK,
            profile_bindings=self.profile_bindings,
            messages=[
                ChatMessage(role="system", content=_SYSTEM),
                ChatMessage(role="user", content=_USER.format(query=query, passages=passages)),
            ],
            response_format={"type": "json_object"},
            temperature=self.temperature,
            purpose=self.purpose,
        )
        content = response.choices[0].message.content if response.choices else ""
        try:
            judgement = parse_judgement(_loads_lenient(content), len(hits))
        except ValueError as exc:
            # Not valid JSON at all.
            judgement = Judgement([], f"the reply is not JSON: {exc}")
        if judgement.malformed is not None:
            # An unintelligible reply is NOT an abstention. Left to fall through
            # to `apply_ranking` with an empty list it empties the results and
            # is indistinguishable from "the model judged nothing relevant" — a
            # broken reranker reporting perfect humility. Measured on a weak
            # model, that difference is nDCG@10 0.970 vs 0.133.
            _log.warning(
                "rks.rerank.unparseable",
                backend=self.name,
                error=judgement.malformed,
                candidates=len(hits),
                # The reply itself, truncated. "The reply was unreadable"
                # without the reply is a diagnosis nobody can act on: the two
                # notation failures this parser now handles (`{"id": [4]}` and
                # a reply wrapped in a fence) were BOTH found by reading raw
                # replies off a probe script, because this line did not carry
                # them. It contains passage indices and integer scores, so
                # there is nothing here to redact.
                reply=(content or "")[:200],
                impact="fused order kept; this is NOT read as an abstention",
            )
            # Recorded on the RERANKER, not only in the log, so `search_corpus`
            # can put it on the span. Without this, a search whose reranker
            # produced garbage is byte-identical on the trace to one whose
            # reranker did real work: same `backend`, same `returned`,
            # `abstained=False`, no `skipped` attribute. An operator reading the
            # trace cannot tell "it reordered my results" from "its reply was
            # unreadable and I got fusion order" — and `_loads_lenient`'s own
            # docstring says a strict parse "failed on the large majority of
            # replies" on this deployment, so it is the common case.
            self.skipped_reason = f"{self.name} reply unreadable: {judgement.malformed}"
            return list(hits[:top_k])
        if judgement.declined:
            # Said out loud, at info rather than warning: this is the feature
            # working. It shares a shape with the failure above — an empty
            # result — and the two were indistinguishable on a trace, so the
            # one that is correct behaviour gets a line of its own rather than
            # being read later as "retrieval returned nothing again".
            _log.info(
                "rks.rerank.declined",
                backend=self.name,
                candidates=len(hits),
                impact="every candidate scored below the relevance floor; "
                "returning nothing is the answer, not a failure",
            )
        return apply_ranking(hits, judgement.scores, top_k=top_k, keep_unranked=self.keep_unranked)


@dataclass
class AdaptiveReranker:
    """Try the cross-encoder once; fall back to the LLM and remember.

    The probe is per process and per model, not per search: a gateway that does
    not serve a reranker would otherwise cost one wasted round trip on every
    query, and the answer does not change between two searches a millisecond
    apart.
    """

    native: CrossEncoderReranker
    fallback: ListwiseLlmReranker
    name: str = "adaptive"
    skipped_reason: str | None = None
    #: `None` = not yet probed, `True` = the gateway reranks, `False` = it does
    #: not and never will for this model.
    _native_supported: bool | None = None

    async def rank(self, *, query: str, hits: Sequence[ChunkHit], top_k: int) -> list[ChunkHit]:
        if self._native_supported is not False:
            try:
                out = await self.native.rank(query=query, hits=hits, top_k=top_k)
            except Exception as exc:
                # ANY failure of the cross-encoder path, not only the 4xx that
                # `RerankUnsupported` is written for.
                #
                # The reference gateway answers `POST /v1/rerank` with **500**
                # "litellm.APIConnectionError: Unsupported provider:
                # bedrock_mantle" — semantically "this model cannot rerank",
                # syntactically a server error. Catching only `RerankUnsupported`
                # meant the fallback that exists precisely for this deployment
                # never ran on it: the exception escaped, and `search_corpus`
                # degraded the whole search to fused order rather than using the
                # LLM reranker sitting right there.
                #
                # The blast radius of being wrong in this direction is small and
                # bounded: the fallback IS a reranker, and it is tried once per
                # process because `_native_supported` latches. Being wrong in the
                # other direction costs the feature entirely.
                self._native_supported = False
                _log.warning(
                    "rks.rerank.native_unsupported",
                    reason=f"{type(exc).__name__}: {exc}",
                    expected=isinstance(exc, RerankUnsupported),
                    fallback=self.fallback.name,
                    impact="every later search in this process uses the LLM reranker",
                )
            else:
                self._native_supported = True
                self.name = self.native.name
                return out
        self.name = self.fallback.name
        out = await self.fallback.rank(query=query, hits=hits, top_k=top_k)
        # The delegate's per-call verdict, carried out to the caller.
        #
        # `_search_and_rerank` re-reads `reranker.skipped_reason` after the call
        # precisely so a reranker that degraded DURING `rank` — an unreadable
        # reply, a cross-encoder that answered with nothing — lands on the span.
        # That read was against THIS object, whose own field nothing ever set,
        # so on the only path production takes it could never be anything but
        # `None`: the defence was written, tested on the delegate, and inert on
        # the wrapper. Copied rather than delegated through a property because
        # the `Reranker` protocol declares a plain attribute.
        self.skipped_reason = self.fallback.skipped_reason
        return out


#: Said in words, once, so the same sentence reaches the span, the log and any
#: operator reading either. "rerank is off" without a reason is the shape of
#: silence this workstream exists to remove.
REASON_UNBOUND = (
    "no 'rerank' capability is bound on this project's model profile, so "
    "results are in fusion order only"
)
REASON_NOT_REQUESTED = "the caller did not ask for reranking"


def reranker_for(
    *,
    client: LiteLLMClient,
    principal: Principal,
    project_id: UUID,
    profile_bindings: dict[str, Any],
    agent_run_id: UUID | None = None,
    keep_unranked: bool = True,
) -> Reranker:
    """The reranker this project's profile earns — or a :class:`NoReranker`.

    Never raises and never returns ``None``: an unbound capability is a normal
    state (Aleph ships no model list, so a fresh project binds nothing until
    autoconfigure runs) and it must degrade to fused order with a reason, the
    same way an unbound embedder degrades to lexical-only rather than to an
    empty index.
    """
    try:
        resolve_binding(profile_bindings, Capability.RERANK)
    except ValidationFailed:
        return NoReranker(skipped_reason=REASON_UNBOUND)
    return AdaptiveReranker(
        native=CrossEncoderReranker(
            client=client,
            principal=principal,
            project_id=project_id,
            profile_bindings=profile_bindings,
            agent_run_id=agent_run_id,
            keep_unranked=keep_unranked,
        ),
        fallback=ListwiseLlmReranker(
            client=client,
            principal=principal,
            project_id=project_id,
            profile_bindings=profile_bindings,
            agent_run_id=agent_run_id,
            keep_unranked=keep_unranked,
        ),
    )

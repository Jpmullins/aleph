"""LiteLLMClient — the single LLM transport chokepoint.

Every LLM and embedding call in Aleph routes through this client. Agent
also routes through the gateway via `_type: openai` configs pointed at
`LITELLM_BASE_URL`. No provider SDK is called directly anywhere else.

The client:
  1. Resolves capability → ModelBinding from a ModelProfile.
  2. Wraps the HTTP call in an OTEL span tagged with project_id,
     capability, model, purpose.
  3. Applies the tenacity retry policy.
  4. Writes ModelCall + CostLedgerEvent inside one transaction.
  5. Honors an optional idempotency_key by checking Redis.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast
from uuid import UUID, uuid4

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, field_validator

from aleph_core.errors import GatewayUnavailable, ValidationFailed
from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_models.auth import gateway_auth_headers
from aleph_models.limiter import GatewayLimiter, limiter_for
from aleph_models.pricing import PricingTable
from aleph_models.profile import resolve_binding
from aleph_models.retry import gateway_retry
from aleph_models.urls import gateway_origin
from aleph_observability.metrics import record_llm_request, record_llm_usage
from aleph_observability.tracing import current_trace_id, start_span

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from aleph_security.principal import Principal


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------

Role = Literal["system", "user", "assistant", "tool"]


#: How much of a gateway error body survives into the exception message.
#: Bounded because a gateway that echoes the offending payload would otherwise
#: put an entire prompt into a log line.
_ERROR_BODY_CHARS = 2000


class ChatMessage(BaseModel):
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _null_content_is_empty(cls, v: object) -> object:
        """A completion with no content is empty, not invalid.

        OpenAI-compatible servers send ``content: null`` for a choice that
        produced no assistant text — a completion truncated by ``max_tokens``
        mid-reasoning, or a turn that is purely tool calls. Both are normal, and
        both would otherwise fail validation on a non-optional ``str`` and take
        down the whole call rather than the turn.

        Coercing to "" keeps the field non-optional for the many call sites that
        treat content as text. Truncation stays detectable: it is carried by
        ``finish_reason == "length"``, which is where that signal belongs.
        """
        return "" if v is None else v


class ToolSchema(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["function"] = "function"
    function: dict[str, Any]


class ChatChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: str | None = None


class ChatUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: dict[str, Any] | None = None


class ChatResponse(BaseModel):
    id: str
    model: str
    choices: list[ChatChoice]
    usage: ChatUsage
    cost_usd: str  # decimal as string for JSON-safety
    cache_savings_usd: str
    latency_ms: int
    model_call_id: str
    trace_id: str | None = None


class EmbedResponse(BaseModel):
    model: str
    embeddings: list[list[float]]
    input_tokens: int
    cost_usd: str
    latency_ms: int
    model_call_id: str
    trace_id: str | None = None


class RerankResult(BaseModel):
    """One reranked document: its position in the INPUT list, and its score.

    ``index`` refers to the caller's ``documents`` list, not to any ordering the
    gateway invented — a reranker that returned scores without the index they
    belong to would be unusable, and Cohere-shaped responses omit the documents
    themselves by default.
    """

    index: int
    relevance_score: float


class RerankResponse(BaseModel):
    model: str
    #: Best first. Only the documents the gateway chose to return: `top_n` may
    #: be smaller than the input, so absence is not a score of zero.
    results: list[RerankResult]
    latency_ms: int
    model_call_id: str
    trace_id: str | None = None


class RerankUnsupported(Exception):
    """The gateway answered, and the bound model cannot rerank.

    Distinct from :class:`aleph_core.errors.GatewayUnavailable` because the two
    need opposite responses: a gateway that is down should be retried, and a
    model that is not a reranker should never be sent there again. Aleph's own
    deployment is the second case — ``POST /v1/rerank`` is routed (it validates
    the model name rather than 404-ing) and no model the key can reach serves
    ``mode="rerank"`` — so this is the path that actually runs, not a corner
    case. :mod:`aleph_rks.rerank` catches it and falls back to the LLM
    reranker, once, remembering the answer.
    """


# ---------------------------------------------------------------------------
# Idempotency cache
# ---------------------------------------------------------------------------


@dataclass
class _IdemCache:
    redis: Redis | None

    async def get(self, key: str) -> str | None:
        if self.redis is None:
            return None
        raw = await self.redis.get(f"aleph:idem:{key}")
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    async def put(self, key: str, model_call_id: str, ttl_seconds: int) -> None:
        if self.redis is None:
            return
        await self.redis.set(f"aleph:idem:{key}", model_call_id, ex=ttl_seconds)


#: Keys under which a gateway/provider may report cache-WRITE tokens.
#:
#: Anthropic names it `cache_creation_input_tokens`; LiteLLM passes that through
#: at the top level and also mirrors some providers into
#: `prompt_tokens_details`. Accepting every known spelling matters because the
#: failure is silent: an unrecognised key means the write is billed as an
#: ordinary uncached token and the call under-reports its cost.
_CACHE_WRITE_KEYS = (
    "cache_creation_input_tokens",
    "cache_write_tokens",
    "cached_write_tokens",
)

_log = structlog.get_logger(__name__)


def _warn_unpriced(model: str, capability: str, purpose: str) -> None:
    """Shout about a call we could not price.

    Rule 5 says every LLM call writes a `ModelCall` + `CostLedgerEvent`. It
    does not say the number has to be *right*, and for a while it was not: an
    unrecognised model silently cost `$0`, so a pricing table that matched
    none of the gateway's model names still produced a full ledger reading
    $0.00. The row is still written — losing it would be worse — but it is
    marked `pricing_source="unknown"` and announced here, because a spend
    dashboard that is quietly wrong is more dangerous than one that is empty.
    """
    _log.error(
        "model_call.unpriced",
        model=model,
        capability=capability,
        purpose=purpose,
        remediation=(
            "model is absent from the pricing table; run gateway discovery so "
            "rates are read from /model/info, or bind a model the gateway prices"
        ),
    )


#: Statuses from ``/v1/rerank`` that mean "this model is not a reranker".
#:
#: 400 is what the deployed gateway actually returns for a chat model
#: ("Invalid model name passed in model=…"); 404 is what a gateway with no
#: rerank route at all returns; 422 and 501 are the other shapes seen in the
#: OpenAI-compatible ecosystem. 401/403/429/5xx are absent on purpose — those
#: are outages or credential problems and must not be reported as a capability
#: gap, or an operator goes and edits the model profile to fix an expired key.
_RERANK_UNSUPPORTED_STATUSES = frozenset({400, 404, 422, 501})


def _rerank_input_tokens(body: dict[str, Any]) -> int:
    """Tokens the gateway says it read, in whichever place it put them.

    Rerank responses have no agreed usage shape: LiteLLM passes Cohere's
    ``meta.billed_units.search_units`` through untouched, some backends emit an
    OpenAI-style ``usage.prompt_tokens``, and plenty emit nothing. Zero is the
    honest answer for the last case — the row is still written, and
    `pricing_source` records that the number is not from the gateway.
    """
    usage = body.get("usage")
    if isinstance(usage, dict):
        raw = cast("dict[str, object]", usage).get("prompt_tokens")
        if raw is not None:
            return int(raw)  # type: ignore[arg-type]
    meta = body.get("meta")
    if isinstance(meta, dict):
        billed = cast("dict[str, object]", meta).get("billed_units")
        if isinstance(billed, dict):
            raw = cast("dict[str, object]", billed).get("input_tokens")
            if raw is not None:
                return int(raw)  # type: ignore[arg-type]
    return 0


def _rerank_results(body: dict[str, Any], document_count: int) -> list[RerankResult]:
    """Parse the results array, dropping anything that cannot address a document.

    An out-of-range or missing ``index`` is dropped rather than clamped. The
    index is the ONLY link back to the caller's list, so a wrong one silently
    reorders the wrong passage — which looks exactly like a reranker that made a
    bad judgement and is impossible to tell apart downstream.
    """
    raw = body.get("results")
    if not isinstance(raw, list):
        return []
    out: list[RerankResult] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, dict):
            continue
        row = cast("dict[str, object]", item)
        index = row.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if not 0 <= index < document_count:
            continue
        score = row.get("relevance_score", row.get("score"))
        if not isinstance(score, int | float) or isinstance(score, bool):
            continue
        out.append(RerankResult(index=index, relevance_score=float(score)))
    return out


def _cache_write_tokens(usage: object, details: object) -> int:
    """Cache-write token count from whichever key the gateway used."""
    for source in (usage, details):
        if not isinstance(source, dict):
            continue
        mapping = cast("dict[str, object]", source)
        for key in _CACHE_WRITE_KEYS:
            raw = mapping.get(key)
            if raw is not None:
                return int(raw)  # type: ignore[arg-type]
    return 0


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LiteLLMClient:
    """OpenAI-compatible client pointed at the Insights LiteLLM Gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        http_client: httpx.AsyncClient,
        pricing: PricingTable,
        session_maker: async_sessionmaker[AsyncSession],
        redis_client: Redis | None = None,
        idempotency_ttl_seconds: int = 86_400,
        limiter: GatewayLimiter | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if not base_url:
            msg = "LITELLM_BASE_URL is required"
            raise ValidationFailed(msg)
        if not api_key:
            msg = "INSIGHTS_LITELLM_API_KEY is required"
            raise ValidationFailed(msg)

        # A BARE ORIGIN. This class builds `{base}/v1/...` itself, so a
        # configured value that already carries `/v1` — which is the form every
        # vLLM/Ollama quickstart prints, because it is what the OpenAI SDK wants
        # — would produce `/v1/v1/chat/completions`. See `aleph_models.urls`.
        self._base_url = gateway_origin(base_url)
        self._api_key = api_key
        self._http = http_client
        self._pricing = pricing
        if not pricing.models():
            # Said out loud at CONSTRUCTION, because the consequence is silent
            # and lands in an append-only ledger. Every `ModelCall` this client
            # writes will carry `pricing_source="unknown"` and `cost_usd=0` —
            # "this call was free" — and those rows are what
            # `status_numbers.py` counts as uncosted.
            #
            # Not an error: `copilot_cost_callback` deliberately falls back to
            # an empty table when the kernel has not bound one yet, and a fresh
            # process before discovery genuinely knows no rates. But a caller
            # that MEANT to pass rates and passed `PricingTable()` gets nothing
            # back today except a health number moving next week. The retrieval
            # eval did exactly that and wrote 90 unpriced rows.
            _log.warning(
                "litellm.pricing_table_empty",
                base_url=self._base_url,
                impact=(
                    "every ModelCall this client writes will be pricing_source="
                    "'unknown' with cost_usd=0. Build it with "
                    "PricingTable.from_discovery(...)"
                ),
            )
        self._session_maker = session_maker
        self._idem = _IdemCache(redis=redis_client)
        self._idem_ttl = idempotency_ttl_seconds
        # Defaulted, not required. A limiter that only applies when someone
        # remembers to pass one is not a ceiling — and there are call sites
        # (`aleph_models.autoconfigure` → `probe_model`) that are handed an HTTP
        # client and know nothing about limits. `limiter_for` is keyed by
        # endpoint, so every client built against the same gateway shares one
        # door whether or not it was given one.
        self._limiter = limiter or limiter_for(base_url)
        # Test seam only: proving a `Retry-After: 7` is honoured costs seven
        # seconds of wall clock otherwise, which is why it was never proved.
        self._retry_sleep = retry_sleep

    # ---- public API --------------------------------------------------------

    async def health(self) -> bool:
        """Is the gateway answering? Deliberately NOT behind the concurrency door.

        `/readyz` calls this, compose wires `/readyz` as the API's healthcheck,
        and Docker restarts a container that fails it. Queueing a health probe
        behind eight in-flight agent calls would report "the gateway is down"
        whenever the gateway is merely busy, and the cure — restarting Aleph —
        does nothing about somebody else's endpoint while taking the whole stack
        with it. The frequency of this probe is bounded where it belongs, by the
        30s cached leg in `routes/health.py`, not by a ceiling meant to protect
        the gateway from Aleph's fan-out.
        """
        try:
            resp = await self._http.get(
                f"{self._base_url}/v1/models",
                headers=gateway_auth_headers(self._api_key),
                timeout=5.0,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        async with self._limiter.slot(purpose="list_models"):
            resp = await self._http.get(
                f"{self._base_url}/v1/models",
                headers=gateway_auth_headers(self._api_key),
                timeout=10.0,
            )
        if resp.status_code != 200:
            msg = f"litellm gateway list_models failed: {resp.status_code}"
            raise GatewayUnavailable(msg)
        body = resp.json()
        return [m["id"] for m in body.get("data", [])]

    async def chat(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        capability: Capability,
        profile_bindings: dict[str, Any],
        messages: list[ChatMessage],
        response_format: dict[str, Any] | None = None,
        tools: list[ToolSchema] | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        purpose: str,
        idempotency_key: str | None = None,
    ) -> ChatResponse:
        del principal  # used by callers; not needed here once project_id is bound
        binding = resolve_binding(profile_bindings, capability)

        if idempotency_key:
            cached = await self._idem.get(idempotency_key)
            if cached is not None:
                # Replay path returns a stub-shaped response; callers usually
                # fetch the original ModelCall row out-of-band. We surface
                # the original model_call_id for them to inspect.
                return ChatResponse(
                    id=f"idem:{idempotency_key}",
                    model=binding.model,
                    choices=[
                        ChatChoice(
                            index=0,
                            message=ChatMessage(role="assistant", content=""),
                            finish_reason="idempotent_replay",
                        )
                    ],
                    usage=ChatUsage(),
                    cost_usd="0",
                    cache_savings_usd="0",
                    latency_ms=0,
                    model_call_id=cached,
                    trace_id=current_trace_id(),
                )

        payload: dict[str, Any] = {
            "model": binding.model,
            "messages": [m.model_dump(exclude_none=True) for m in messages],
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        if tools is not None:
            payload["tools"] = [t.model_dump() for t in tools]

        attrs = {
            "aleph.project_id": str(project_id),
            "aleph.capability": capability.value,
            "aleph.model": binding.model,
            "aleph.purpose": purpose,
            "gen_ai.system": "litellm",
            "gen_ai.request.model": binding.model,
        }

        started_at = time.monotonic()
        with start_span("litellm.chat", **attrs) as span:
            # WS-P9. The failure path is counted too, and that is the whole
            # point: a counter that only moves on success cannot tell "the
            # gateway is down" from "nobody is calling it", and those need
            # opposite responses. `aleph_llm_requests_total{purpose,outcome}`
            # is also the series that characterises backlog E5 ("weirdly rate
            # limited") — per-purpose request rate is exactly the number that
            # confirms or drops the subagent fan-out hypothesis.
            try:
                body = await self._post_with_retry("/v1/chat/completions", payload)
            except Exception:
                record_llm_request(
                    capability=capability.value,
                    purpose=purpose,
                    outcome="error",
                    duration_s=time.monotonic() - started_at,
                )
                raise
            elapsed_s = time.monotonic() - started_at
            latency_ms = int(elapsed_s * 1000)
            record_llm_request(
                capability=capability.value,
                purpose=purpose,
                outcome="ok",
                duration_s=elapsed_s,
            )

            usage = body.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            cached_tokens = 0
            details = usage.get("prompt_tokens_details") or {}
            if isinstance(details, dict):
                cached_tokens = int(details.get("cached_tokens", 0))
            # Cache WRITES are billed at a premium and are reported separately
            # from reads. Different gateway/provider shapes surface them under
            # different keys, so accept the known spellings rather than silently
            # treating a write as a free uncached token.
            cache_write_tokens = _cache_write_tokens(usage, details)

            priced = self._pricing.breakdown(
                model=binding.model,
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                completion_tokens=completion_tokens,
                cache_write_tokens=cache_write_tokens,
            )
            cost, savings = priced.cost_usd, priced.cache_savings_usd
            if not priced.priced:
                _warn_unpriced(binding.model, capability.value, purpose)

            # The label that makes "what share of our spend is unpriced" one
            # query instead of an anecdote — backlog E4.
            record_llm_usage(
                capability=capability.value,
                purpose=purpose,
                pricing_source=priced.source,
                input_tokens=input_tokens,
                output_tokens=completion_tokens,
                cost_usd=cost,
            )
            span.set_attribute("aleph.pricing_source", priced.source)
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            span.set_attribute("gen_ai.usage.cached_tokens", cached_tokens)
            span.set_attribute("gen_ai.usage.cache_write_tokens", cache_write_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
            span.set_attribute("aleph.cost_usd", float(cost))
            span.set_attribute("aleph.latency_ms", latency_ms)
            trace_id = current_trace_id()

            model_call_id = await self._record_call(
                project_id=project_id,
                agent_run_id=agent_run_id,
                capability=capability.value,
                model=binding.model,
                purpose=purpose,
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
                completion_tokens=completion_tokens,
                cost=cost,
                savings=savings,
                pricing_source=priced.source,
                input_rate_usd=priced.input_rate_usd,
                output_rate_usd=priced.output_rate_usd,
                latency_ms=latency_ms,
                trace_id=trace_id,
                timestamp=utcnow(),
            )

        if idempotency_key:
            await self._idem.put(idempotency_key, str(model_call_id), self._idem_ttl)

        choices = [
            ChatChoice(
                index=int(c.get("index", i)),
                message=ChatMessage.model_validate(c["message"]),
                finish_reason=c.get("finish_reason"),
            )
            for i, c in enumerate(body.get("choices", []))
        ]
        return ChatResponse(
            id=str(body.get("id", "")),
            model=binding.model,
            choices=choices,
            usage=ChatUsage(
                prompt_tokens=input_tokens,
                completion_tokens=completion_tokens,
                total_tokens=input_tokens + completion_tokens,
                prompt_tokens_details=details if isinstance(details, dict) else None,
            ),
            cost_usd=str(cost),
            cache_savings_usd=str(savings),
            latency_ms=latency_ms,
            model_call_id=str(model_call_id),
            trace_id=trace_id,
        )

    async def embed(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        profile_bindings: dict[str, Any],
        input: list[str],
        purpose: str,
    ) -> EmbedResponse:
        del principal
        binding = resolve_binding(profile_bindings, Capability.EMBEDDING)
        payload = {"model": binding.model, "input": input}

        attrs = {
            "aleph.project_id": str(project_id),
            "aleph.capability": Capability.EMBEDDING.value,
            "aleph.model": binding.model,
            "aleph.purpose": purpose,
            "gen_ai.system": "litellm",
            "gen_ai.request.model": binding.model,
        }

        started_at = time.monotonic()
        with start_span("litellm.embed", **attrs) as span:
            # See `chat` — the embedder is the leg whose silent death emptied
            # the whole retrieval index (WS-RS1), and it had no counter.
            try:
                body = await self._post_with_retry("/v1/embeddings", payload)
            except Exception:
                record_llm_request(
                    capability=Capability.EMBEDDING.value,
                    purpose=purpose,
                    outcome="error",
                    duration_s=time.monotonic() - started_at,
                )
                raise
            elapsed_s = time.monotonic() - started_at
            latency_ms = int(elapsed_s * 1000)
            record_llm_request(
                capability=Capability.EMBEDDING.value,
                purpose=purpose,
                outcome="ok",
                duration_s=elapsed_s,
            )

            usage = body.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens", 0))
            priced = self._pricing.breakdown(
                model=binding.model,
                input_tokens=input_tokens,
                cached_tokens=0,
                completion_tokens=0,
            )
            cost, savings = priced.cost_usd, priced.cache_savings_usd
            if not priced.priced:
                _warn_unpriced(binding.model, Capability.EMBEDDING.value, purpose)
            record_llm_usage(
                capability=Capability.EMBEDDING.value,
                purpose=purpose,
                pricing_source=priced.source,
                input_tokens=input_tokens,
                output_tokens=0,
                cost_usd=cost,
            )
            span.set_attribute("aleph.pricing_source", priced.source)
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            span.set_attribute("aleph.cost_usd", float(cost))
            trace_id = current_trace_id()

            model_call_id = await self._record_call(
                project_id=project_id,
                agent_run_id=agent_run_id,
                capability=Capability.EMBEDDING.value,
                model=binding.model,
                purpose=purpose,
                input_tokens=input_tokens,
                cached_tokens=0,
                cache_write_tokens=0,
                completion_tokens=0,
                cost=cost,
                savings=savings,
                pricing_source=priced.source,
                input_rate_usd=priced.input_rate_usd,
                output_rate_usd=priced.output_rate_usd,
                latency_ms=latency_ms,
                trace_id=trace_id,
                timestamp=utcnow(),
            )

        embeddings = [item["embedding"] for item in body.get("data", [])]
        return EmbedResponse(
            model=binding.model,
            embeddings=embeddings,
            input_tokens=input_tokens,
            cost_usd=str(cost),
            latency_ms=latency_ms,
            model_call_id=str(model_call_id),
            trace_id=trace_id,
        )

    async def rerank(
        self,
        *,
        principal: Principal,
        project_id: UUID,
        agent_run_id: UUID | None,
        profile_bindings: dict[str, Any],
        query: str,
        documents: list[str],
        top_n: int | None = None,
        purpose: str,
    ) -> RerankResponse:
        """Cross-encoder rerank via ``POST /v1/rerank``.

        The third verb on this client, and the first one whose model may not
        exist. ``Capability.RERANK`` has been an enum member with no
        implementation since it was written; this is the transport half.

        **A 4xx from this route is a statement about the model, not the
        gateway.** The configured endpoint routes ``/v1/rerank`` — it answers
        400 "Invalid model name passed in model=…" rather than 404 — and none
        of the 26 models the key can reach is a reranker. So the honest
        outcome for a non-rerank model is :class:`RerankUnsupported`, which
        callers can act on, rather than a generic transport error that looks
        like an outage and gets retried.

        401/403 are deliberately NOT translated: an expired key is an outage,
        and reporting it as "this model cannot rerank" would send an operator
        to the model profile to fix a credential.
        """
        del principal
        binding = resolve_binding(profile_bindings, Capability.RERANK)
        payload: dict[str, Any] = {
            "model": binding.model,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n

        attrs = {
            "aleph.project_id": str(project_id),
            "aleph.capability": Capability.RERANK.value,
            "aleph.model": binding.model,
            "aleph.purpose": purpose,
            "aleph.rerank.documents": len(documents),
            "gen_ai.system": "litellm",
            "gen_ai.request.model": binding.model,
        }

        started_at = time.monotonic()
        with start_span("litellm.rerank", **attrs) as span:
            try:
                body = await self._post_with_retry("/v1/rerank", payload)
            except httpx.HTTPStatusError as exc:
                record_llm_request(
                    capability=Capability.RERANK.value,
                    purpose=purpose,
                    outcome="error",
                    duration_s=time.monotonic() - started_at,
                )
                if exc.response.status_code in _RERANK_UNSUPPORTED_STATUSES:
                    msg = (
                        f"the gateway will not rerank with '{binding.model}': "
                        f"HTTP {exc.response.status_code} {exc.response.text[:300]}"
                    )
                    raise RerankUnsupported(msg) from exc
                raise
            except Exception:
                record_llm_request(
                    capability=Capability.RERANK.value,
                    purpose=purpose,
                    outcome="error",
                    duration_s=time.monotonic() - started_at,
                )
                raise
            elapsed_s = time.monotonic() - started_at
            latency_ms = int(elapsed_s * 1000)
            record_llm_request(
                capability=Capability.RERANK.value,
                purpose=purpose,
                outcome="ok",
                duration_s=elapsed_s,
            )

            # Rerank endpoints bill in "search units", not tokens, and most
            # report nothing at all. The ModelCall row is written regardless —
            # rule 5 is that every gateway call is ledgered, and a call that
            # writes no row is a call that cannot be found later. An unpriced
            # one is marked `unknown`, never a silent $0.
            input_tokens = _rerank_input_tokens(body)
            priced = self._pricing.breakdown(
                model=binding.model,
                input_tokens=input_tokens,
                cached_tokens=0,
                completion_tokens=0,
            )
            cost, savings = priced.cost_usd, priced.cache_savings_usd
            if not priced.priced:
                _warn_unpriced(binding.model, Capability.RERANK.value, purpose)
            record_llm_usage(
                capability=Capability.RERANK.value,
                purpose=purpose,
                pricing_source=priced.source,
                input_tokens=input_tokens,
                output_tokens=0,
                cost_usd=cost,
            )
            span.set_attribute("aleph.pricing_source", priced.source)
            span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
            span.set_attribute("aleph.cost_usd", float(cost))
            span.set_attribute("aleph.latency_ms", latency_ms)
            trace_id = current_trace_id()

            model_call_id = await self._record_call(
                project_id=project_id,
                agent_run_id=agent_run_id,
                capability=Capability.RERANK.value,
                model=binding.model,
                purpose=purpose,
                input_tokens=input_tokens,
                cached_tokens=0,
                cache_write_tokens=0,
                completion_tokens=0,
                cost=cost,
                savings=savings,
                pricing_source=priced.source,
                input_rate_usd=priced.input_rate_usd,
                output_rate_usd=priced.output_rate_usd,
                latency_ms=latency_ms,
                trace_id=trace_id,
                timestamp=utcnow(),
            )

        return RerankResponse(
            model=binding.model,
            results=_rerank_results(body, len(documents)),
            latency_ms=latency_ms,
            model_call_id=str(model_call_id),
            trace_id=trace_id,
        )

    # ---- internals ---------------------------------------------------------

    async def _post_with_retry(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST, retrying transport/429/5xx, and learning what the model refuses.

        The learning half exists because a gateway is the authority on what its
        models accept and Aleph is not. Measured on this instance:
        `claude-opus-4-7` reaches Bedrock, which answers
        400 with "temperature is deprecated for this model". 1,072 model
        profiles bind that model and 15 call sites pass a hardcoded
        `temperature`, so every one of those combinations was a hard failure —
        `assistant.compose` among them, which is why the retrieval debug route
        returned 500.

        Fixing the 15 call sites would be the wrong shape: the next model to
        drop a sampling knob would break them all again, and the knowledge
        belongs where the gateway's answer arrives. So the parameter is dropped
        and the call retried ONCE, and the pairing is remembered per process so
        it costs one wasted request per model rather than one per call.

        Only pure sampling knobs are droppable (`_DROPPABLE_PARAMS`). They
        change how random the answer is, not what was asked for, so removing
        one cannot silently alter the contract of the request the caller made.
        """
        model = str(payload.get("model", ""))
        payload = _without_known_unsupported(self._base_url, model, payload)
        try:
            return await self._post_once(path, payload)
        except httpx.HTTPStatusError as exc:
            param = _unsupported_param(exc, payload)
            if param is None:
                raise
            _remember_unsupported(self._base_url, model, param)
            _log.warning(
                "gateway.param_unsupported",
                model=model,
                param=param,
                path=path,
                impact=(
                    "dropped and retried; every later call for this model in this process omits it"
                ),
            )
            return await self._post_once(path, {k: v for k, v in payload.items() if k != param})

    async def _post_once(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            **gateway_auth_headers(self._api_key),
            "Content-Type": "application/json",
            "X-Aleph-Request-Id": str(uuid4()),
        }
        url = f"{self._base_url}{path}"

        # The slot is taken per ATTEMPT, not per call: a retry is another
        # request arriving at the same endpoint, and a limiter that counts the
        # first attempt only under-reports exactly when the gateway is already
        # over its budget.
        async for attempt in gateway_retry(sleep=self._retry_sleep):
            with attempt:
                async with self._limiter.slot(purpose=path):
                    resp = await self._http.post(url, json=payload, headers=headers, timeout=120.0)
                    if resp.status_code >= 400:
                        _raise_with_gateway_reason(resp, url)
                    return resp.json()
        # gateway_retry().reraise=True guarantees we raise instead of falling through.
        msg = "gateway retry exhausted"
        raise GatewayUnavailable(msg)

    async def _record_call(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID | None,
        capability: str,
        model: str,
        purpose: str,
        input_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        cost,
        savings,
        latency_ms: int,
        trace_id: str | None,
        timestamp: datetime,
        cache_write_tokens: int = 0,
        pricing_source: str = "unknown",
        input_rate_usd: Decimal = Decimal("0"),
        output_rate_usd: Decimal = Decimal("0"),
    ) -> UUID:
        # Import here to avoid a hard dependency on aleph_db at module import.
        from aleph_db.repos.cost import CostWriter as _CostWriter

        async with self._session_maker() as session:
            writer = _CostWriter(session)
            call = await writer.record_call(
                project_id=project_id,
                agent_run_id=agent_run_id,
                capability=capability,
                model=model,
                purpose=purpose,
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                cache_write_tokens=cache_write_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost,
                cache_savings_usd=savings,
                pricing_source=pricing_source,
                input_rate_usd=input_rate_usd,
                output_rate_usd=output_rate_usd,
                latency_ms=latency_ms,
                trace_id=trace_id,
                timestamp=timestamp,
            )
            await session.commit()
            return call.id


def _raise_with_gateway_reason(resp: httpx.Response, url: str) -> NoReturn:
    """Raise like `raise_for_status()`, but keep the reason the gateway gave.

    `resp.raise_for_status()` builds its message from the status code and the
    URL alone and drops the response BODY, which on an OpenAI-compatible
    gateway is the only place the actual cause appears: an unsupported
    parameter, a context-window overflow, a model the virtual key may not
    reach. Measured on this instance, a `400 Bad Request` from
    `assistant.compose` produced a full traceback in which no line said what was
    wrong with the request, and reproducing it by hand was the only way to find
    out.

    The exception TYPE and its `response` are unchanged, because
    `aleph_models.retry._is_retryable` decides on `exc.response.status_code` and
    a different type here would silently stop 429s and 5xxs from being retried.

    The body is truncated: a gateway that echoes the offending payload back
    would otherwise put an entire prompt into a log line.
    """
    detail = ""
    try:
        body = resp.text
    except Exception:  # pragma: no cover - a body that cannot be decoded
        body = ""
    if body.strip():
        detail = f" — gateway said: {body.strip()[:_ERROR_BODY_CHARS]}"
    msg = f"{resp.status_code} {resp.reason_phrase} from {url}{detail}"
    raise httpx.HTTPStatusError(msg, request=resp.request, response=resp)


#: Parameters safe to drop when a gateway says the model refuses them.
#:
#: Every one is a pure sampling knob: it changes how random the answer is, not
#: what was asked for. Dropping `messages`, `model` or `response_format` could
#: silently change the contract of the request, so a gateway naming one of
#: those is re-raised rather than worked around.
_DROPPABLE_PARAMS: frozenset[str] = frozenset(
    {"temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "seed"}
)

#: Phrases a gateway uses to say "this model will not take that parameter".
_UNSUPPORTED_PHRASES: tuple[str, ...] = (
    "is deprecated for this model",
    "unsupported parameter",
    "unsupported value",
    "does not support",
    "is not supported",
)

#: (base_url, model) -> parameters that model has refused, learned at runtime.
#:
#: Process-local and deliberately not persisted: it describes what a gateway
#: did a moment ago, and a gateway that is reconfigured should be re-learned
#: rather than believed from a cache written last week.
_UNSUPPORTED_SEEN: dict[tuple[str, str], set[str]] = {}


def reset_unsupported_params() -> None:
    """Forget what has been learned. For tests, and for a gateway swap."""
    _UNSUPPORTED_SEEN.clear()


def _remember_unsupported(base_url: str, model: str, param: str) -> None:
    _UNSUPPORTED_SEEN.setdefault((base_url, model), set()).add(param)


def _without_known_unsupported(
    base_url: str, model: str, payload: dict[str, Any]
) -> dict[str, Any]:
    known = _UNSUPPORTED_SEEN.get((base_url, model))
    if not known:
        return payload
    return {k: v for k, v in payload.items() if k not in known}


def _unsupported_param(exc: httpx.HTTPStatusError, payload: dict[str, Any]) -> str | None:
    """Which droppable parameter this error blames, or None.

    Three conditions, all required, because a wrong guess here silently changes
    what was asked: the status is 400, the body reads like an unsupported
    parameter complaint, and the name it quotes is BOTH droppable and actually
    present in the request that was sent. That last check is what stops a
    stray word in an error message from removing something real.
    """
    if exc.response.status_code != 400:
        return None
    try:
        body = exc.response.text
    except Exception:  # pragma: no cover - undecodable body
        return None
    lowered = body.lower()
    if not any(phrase in lowered for phrase in _UNSUPPORTED_PHRASES):
        return None
    for name in _DROPPABLE_PARAMS:
        if name in payload and name in lowered:
            return name
    return None

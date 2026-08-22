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
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, field_validator

from aleph_core.errors import GatewayUnavailable, ValidationFailed
from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_models.limiter import GatewayLimiter, limiter_for
from aleph_models.pricing import PricingTable
from aleph_models.profile import resolve_binding
from aleph_models.retry import gateway_retry
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

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http_client
        self._pricing = pricing
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
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=5.0,
            )
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def list_models(self) -> list[str]:
        async with self._limiter.slot(purpose="list_models"):
            resp = await self._http.get(
                f"{self._base_url}/v1/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
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

    # ---- internals ---------------------------------------------------------

    async def _post_with_retry(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
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
                        resp.raise_for_status()
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

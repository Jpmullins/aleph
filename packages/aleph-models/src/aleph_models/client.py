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
from pydantic import BaseModel, ConfigDict

from aleph_core.errors import GatewayUnavailable, ValidationFailed
from aleph_core.schemas.model_profile import Capability
from aleph_core.time import utcnow
from aleph_models.pricing import PricingTable
from aleph_models.profile import resolve_binding
from aleph_models.retry import gateway_retry
from aleph_observability.tracing import current_trace_id, start_span

if TYPE_CHECKING:
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

    # ---- public API --------------------------------------------------------

    async def health(self) -> bool:
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
            body = await self._post_with_retry("/v1/chat/completions", payload)
            latency_ms = int((time.monotonic() - started_at) * 1000)

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
            body = await self._post_with_retry("/v1/embeddings", payload)
            latency_ms = int((time.monotonic() - started_at) * 1000)

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

        async for attempt in gateway_retry():
            with attempt:
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

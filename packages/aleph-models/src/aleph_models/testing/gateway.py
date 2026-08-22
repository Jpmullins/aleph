"""A gateway that misbehaves on purpose, in process, with no socket.

Every claim Aleph makes about model routing — "it falls back when the key is
restricted", "it refuses to bind a model it could not invoke", "it waits the
Retry-After", "it never exceeds N in flight" — is a claim about how the code
reacts to a *badly behaved* HTTP server. There was no way to write that test.
The nearest thing was pointing a developer's machine at the LiteLLM deployment
of the day, which is the evidence class CLAUDE.md's preamble blames for a
retrieval path that stayed broken across seven work packages: it worked when
somebody tried it.

So this is one shared fake rather than a stub per test. Every misbehaviour
below is a defect this repository actually shipped or actually survived:

* **`/model/info` answers 403.** A LiteLLM *virtual* key — the kind an
  application is issued — is restricted to ``llm_api_routes``. The reference
  deployment refuses the admin route, and discovery must fall back to
  `/v1/models` and label what it fills in from hints. Treating the 403 as fatal
  would leave Settings empty for everyone but an operator.
* **Rates absent.** An unpriced model used to cost `$0` silently, so a pricing
  table matching none of the gateway's names still produced a full ledger
  reading $0.00.
* **A model that lists happily and 400s on invocation.** `bedrock-claude-
  sonnet-4-6` is advertised and then fails because the underlying Bedrock model
  needs an inference-profile ARN. Bound by default, it passes every startup
  check and fails on first use.
* **A model name the gateway does not serve at all.** The profile bound
  `titan-embed-v2`; the gateway serves `titan-embed-text-v2`. Every embed
  400'd, chunks are written only after the embed returns, and
  `document_chunks` sat at 0 rows against 75 ingested sources while nothing
  reported a problem. `DEFAULT_MODELS` deliberately serves the correct name and
  not the wrong one, so a test can reproduce that exact miss.
* **429, with and without `Retry-After`.** The header is optional, and code
  that assumes it is present sleeps zero and hammers a rate-limited gateway.
* **A slow answer**, for timeout and concurrency work.

**The default configuration is the hostile one.** `/model/info` 403s and no
rates are reported unless a test asks for better, via
:meth:`GatewayConfig.well_behaved`. This is the whole point of the design: a
fake that is permissive by default silently weakens every test built on it —
each one would be written against a gateway kinder than the one in production,
pass, and prove nothing about the deployment it is supposed to describe.

Mount it with :class:`httpx.ASGITransport` so no socket opens and no port is
bound. That matters beyond speed: the agent runs in-process inside FastAPI, so
anything on its request path has to be awaited on the same loop, and a fake
that needs a background server cannot be used there at all.

Usage::

    fake = FakeGateway()                       # restricted key, no rates
    async with fake.client() as http:
        models = await discover_models(
            base_url=fake.base_url, api_key=fake.api_key, client=http
        )
    assert fake.count("/model/info") == 1      # tried the good route first
    assert fake.count("/v1/models") == 1       # and fell back

Not imported by any shipped code path: `aleph_models.testing` is a test-only
subpackage and its Starlette dependency lives in the `testing` extra.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

if TYPE_CHECKING:  # pragma: no cover - typing only
    from starlette.types import Receive, Scope, Send

__all__ = [
    "DEFAULT_MODELS",
    "FakeGateway",
    "FakeModel",
    "GatewayConfig",
    "RecordedRequest",
    "ScriptedResponse",
    "rate_limited",
    "server_error",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeModel:
    """One model the fake advertises, and how it behaves when invoked.

    Listing and invoking are separate on purpose. `invoke_error` is the whole
    reason `probe_model` exists: a gateway's model list states configuration,
    not reachability, and the two disagree on every deployment Aleph has been
    pointed at.
    """

    id: str
    #: `chat`, `embedding`, or anything else a gateway might report. Invoking a
    #: model on the wrong route 400s, exactly as a real gateway does — posting
    #: chat messages to an embedder must not look like a working call.
    mode: str = "chat"
    max_input_tokens: int | None = 200_000
    max_output_tokens: int | None = 8_192
    #: Rates as decimal strings so the fixture text carries no float artifact.
    #: ``None`` means *this gateway publishes no rate for this model* — the
    #: honest state for a self-hosted or open-weight model, and the one that
    #: has to stay distinguishable from "free".
    input_per_token: str | None = None
    output_per_token: str | None = None
    cache_read_per_token: str | None = None
    cache_write_per_token: str | None = None
    supports_vision: bool = False
    supports_function_calling: bool = False
    supports_reasoning: bool = False
    supports_prompt_caching: bool = False
    #: The upstream `litellm_params.model`, e.g. `bedrock/anthropic.claude-…`.
    #: Discovery reads the provider prefix off this.
    upstream: str | None = None
    #: Set to make the model list fine and fail on invocation. The message is
    #: returned verbatim so a test can assert the gateway's own words reached
    #: the operator rather than being replaced with "something went wrong".
    invoke_error: str | None = None
    invoke_status: int = 400
    #: Width of the vectors `/v1/embeddings` returns. 1024 matches
    #: `document_chunks.embedding`; a different width is how a dimension
    #: mismatch is reproduced.
    embedding_dim: int = 1024


#: A model set shaped like the deployments Aleph has actually been pointed at,
#: rather than a tidy invented one: two usable chat models at different tiers,
#: one that advertises and cannot be invoked, and a single embedder that the
#: gateway publishes no rate for. The awkward shape is the point — every
#: selection defect this repo has fixed came from one of these rows.
#:
#: The ids are real names on purpose, which has one consequence worth knowing
#: before asserting on prices: `aleph_models.hints` covers some of them, so a
#: model this gateway reports no rate for can still arrive priced — labelled
#: `hints`, never `gateway`. That is the shipped behaviour, not the fake
#: leaking. A test that needs a genuinely unpriced model should name one no
#: hints file claims (`vllm-local-…`).
DEFAULT_MODELS: tuple[FakeModel, ...] = (
    FakeModel(
        id="claude-opus-4-7",
        mode="chat",
        max_input_tokens=200_000,
        max_output_tokens=32_000,
        input_per_token="5.5e-6",
        output_per_token="2.75e-5",
        cache_read_per_token="5.5e-7",
        cache_write_per_token="6.875e-6",
        supports_vision=True,
        supports_function_calling=True,
        supports_reasoning=True,
        supports_prompt_caching=True,
        upstream="anthropic/claude-opus-4-7",
    ),
    FakeModel(
        id="claude-haiku-4-5",
        mode="chat",
        max_input_tokens=200_000,
        max_output_tokens=8_192,
        input_per_token="8e-7",
        output_per_token="4e-6",
        supports_function_calling=True,
        upstream="anthropic/claude-haiku-4-5",
    ),
    # Lists happily; 400s on invocation. The Bedrock inference-profile case.
    FakeModel(
        id="bedrock-claude-sonnet-4-6",
        mode="chat",
        max_input_tokens=200_000,
        max_output_tokens=8_192,
        input_per_token="3e-6",
        output_per_token="1.5e-5",
        supports_function_calling=True,
        upstream="bedrock/anthropic.claude-sonnet-4-6-v1:0",
        invoke_error=(
            "BedrockException - Invocation of model ID "
            "anthropic.claude-sonnet-4-6-v1:0 with on-demand throughput isn't "
            "supported. Retry your request with the ID or ARN of an inference "
            "profile that contains this model."
        ),
    ),
    # The correct embedder name. `titan-embed-v2` — the name the profile bound
    # and no gateway serves — is absent on purpose.
    FakeModel(
        id="titan-embed-text-v2",
        mode="embedding",
        max_input_tokens=8_192,
        max_output_tokens=None,
        embedding_dim=1024,
        upstream="bedrock/amazon.titan-embed-text-v2:0",
    ),
)


@dataclass(frozen=True)
class ScriptedResponse:
    """A canned answer that pre-empts the next invocation request(s).

    Consumed in order by `/v1/chat/completions` and `/v1/embeddings`; once the
    script is exhausted the gateway behaves normally. That ordering is what
    makes "429 then succeed" expressible, which is the only shape that proves a
    retry actually retried rather than the call simply working first time.
    """

    status: int
    body: dict[str, Any] | None = None
    #: Literal `Retry-After` header value, or ``None`` to omit it. Omission is
    #: not an edge case — the header is optional and real gateways skip it.
    retry_after: str | None = None
    #: How many consecutive requests this answer covers.
    times: int = 1
    #: Seconds to stall before answering, on top of `GatewayConfig.latency_s`.
    delay_s: float = 0.0


def rate_limited(*, retry_after: str | None = "2", times: int = 1) -> ScriptedResponse:
    """A 429 shaped like LiteLLM's, with or without `Retry-After`."""
    return ScriptedResponse(
        status=429,
        body={
            "error": {
                "message": "Rate limit reached for the deployment's TPM budget.",
                "type": "rate_limit_error",
                "code": "429",
            }
        },
        retry_after=retry_after,
        times=times,
    )


def server_error(*, status: int = 503, times: int = 1) -> ScriptedResponse:
    """A 5xx — the other retryable class, and the one with no header to read."""
    return ScriptedResponse(
        status=status,
        body={"error": {"message": "upstream provider unavailable", "code": str(status)}},
        times=times,
    )


@dataclass(frozen=True)
class GatewayConfig:
    """What this gateway serves and how badly it behaves.

    **Every default here is the unhelpful answer.** A restricted key that
    refuses `/model/info`, and no rates on anything. Getting a cooperative
    gateway requires saying so — see :meth:`well_behaved` — because a test
    written against a kinder gateway than production has passes that mean
    nothing about production.
    """

    models: tuple[FakeModel, ...] = DEFAULT_MODELS
    api_key: str = "sk-fake-virtual-key"
    #: HOSTILE DEFAULT. False means `/model/info` answers `model_info_status`,
    #: which is what a virtual key restricted to `llm_api_routes` gets.
    model_info_allowed: bool = False
    #: 403 on the reference deployment; 401 on some LiteLLM versions. Both must
    #: fall back, so both are expressible.
    model_info_status: int = 403
    #: HOSTILE DEFAULT. False strips every cost field from `/model/info`, so a
    #: model that *has* rates in its :class:`FakeModel` still arrives unpriced —
    #: the shape of a gateway whose config sets no costs.
    report_rates: bool = False
    #: Canned answers consumed in order before normal invocation handling.
    invoke_script: tuple[ScriptedResponse, ...] = ()
    #: Seconds every request stalls, measured inside the in-flight window so
    #: `peak_in_flight` sees it.
    latency_s: float = 0.0
    #: When False the fake serves anybody. Left True by default because a test
    #: that forgets the bearer token should find out here, not in production.
    require_auth: bool = True
    #: Assistant text returned by `/v1/chat/completions`.
    chat_reply: str = "pong"

    @classmethod
    def well_behaved(cls, **overrides: Any) -> GatewayConfig:
        """An admin key and full rates — everything the default refuses.

        Deliberately a named request rather than a default. Asking for it puts
        the assumption in the test's own text, so a reader can see that this
        test does *not* cover the restricted-key case that production runs in.
        """
        return replace(cls(model_info_allowed=True, report_rates=True), **overrides)


@dataclass(frozen=True)
class RecordedRequest:
    """One request the fake answered, for tests that assert on traffic."""

    method: str
    path: str
    authorization: str | None
    body: dict[str, Any] | None


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


@dataclass
class _Counters:
    """Everything a test can observe about traffic, resettable as one object."""

    requests: list[RecordedRequest] = field(default_factory=list)
    in_flight: int = 0
    peak_in_flight: int = 0


class FakeGateway:
    """An OpenAI-compatible gateway with scriptable defects, mounted in process.

    Counters are per instance, so two fakes in one test are two independent
    endpoints — which is how "a call made under project B's scope reaches B's
    endpoint and never A's" becomes something a test can assert rather than
    something a reviewer has to believe.
    """

    #: Any absolute URL works: `httpx.ASGITransport` ignores the host and hands
    #: the request straight to the app. A `.invalid` TLD is used so a
    #: misconfigured test that escapes the transport fails at DNS instead of
    #: reaching somebody's real gateway.
    base_url: str = "http://fake-gateway.invalid"

    def __init__(self, config: GatewayConfig | None = None) -> None:
        self.config = config or GatewayConfig()
        self._counters = _Counters()
        self._script: deque[ScriptedResponse] = deque(self.config.invoke_script)
        self._models = {m.id: m for m in self.config.models}
        self._starlette = Starlette(
            routes=[
                Route("/model/info", self._model_info, methods=["GET"]),
                Route("/v1/models", self._v1_models, methods=["GET"]),
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
                # Routed, and it 400s a chat model rather than 404-ing. That
                # is exactly what the deployed gateway does — WS-RS6 verified
                # it by hand — and the difference decides whether Aleph reads
                # "this model is not a reranker" or "the gateway is down".
                Route("/v1/rerank", self._rerank, methods=["POST"]),
                # A real gateway 404s an unknown route as JSON, and code that
                # only ever saw a Starlette plaintext 404 mis-reports it.
                Route(
                    "/{rest:path}",
                    self._not_found,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                ),
            ]
        )

    # ---- wiring ------------------------------------------------------------

    @property
    def api_key(self) -> str:
        return self.config.api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point, wrapping the app in the in-flight recorder.

        Counting here rather than inside the handlers is deliberate: the stall
        from `latency_s` has to fall *inside* the in-flight window, or a
        concurrency ceiling test measures nothing.
        """
        if scope["type"] != "http":  # lifespan / websocket — not our business
            await self._starlette(scope, receive, send)
            return
        self._enter()
        try:
            if self.config.latency_s:
                await asyncio.sleep(self.config.latency_s)
            await self._starlette(scope, receive, send)
        finally:
            self._counters.in_flight -= 1

    def transport(self) -> httpx.ASGITransport:
        return httpx.ASGITransport(app=self)

    def client(self, **kwargs: Any) -> httpx.AsyncClient:
        """An `httpx.AsyncClient` wired to this fake. No socket is opened."""
        kwargs.setdefault("base_url", self.base_url)
        return httpx.AsyncClient(transport=self.transport(), **kwargs)

    # ---- observation -------------------------------------------------------

    @property
    def requests(self) -> list[RecordedRequest]:
        return list(self._counters.requests)

    @property
    def request_count(self) -> int:
        return len(self._counters.requests)

    @property
    def peak_in_flight(self) -> int:
        """Highest number of requests handled at once since the last reset."""
        return self._counters.peak_in_flight

    def count(self, path: str) -> int:
        return sum(1 for r in self._counters.requests if r.path == path)

    def models_requested(self) -> list[str]:
        """The `model` field of every invocation, in order.

        The dead-RAG defect was a *name*: the profile asked for a model the
        gateway does not serve. Asserting on the name that went out is how a
        test catches that rather than catching "an error happened".
        """
        out: list[str] = []
        for r in self._counters.requests:
            if r.body is None:
                continue
            model = r.body.get("model")
            if isinstance(model, str):
                out.append(model)
        return out

    def reset(self) -> None:
        """Clear counters and reload the script, without rebuilding the app."""
        self._counters = _Counters()
        self._script = deque(self.config.invoke_script)

    # ---- handlers ----------------------------------------------------------

    async def _model_info(self, request: Request) -> Response:
        await self._record(request)
        denied = self._auth_error(request)
        if denied is not None:
            return denied
        if not self.config.model_info_allowed:
            # Verbatim from the reference deployment. Discovery logs and falls
            # back; anything that raises here breaks every restricted key.
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            "Virtual key is not allowed to call this route. "
                            "Only allowed to call routes: ['llm_api_routes']"
                        ),
                        "type": "auth_error",
                        "code": str(self.config.model_info_status),
                    }
                },
                status_code=self.config.model_info_status,
            )
        return JSONResponse({"data": [self._info_row(m) for m in self.config.models]})

    async def _v1_models(self, request: Request) -> Response:
        await self._record(request)
        denied = self._auth_error(request)
        if denied is not None:
            return denied
        # Ids and nothing else — no mode, no window, no rates. Padding this out
        # would hide the fallback's real cost, which is that hints have to
        # supply everything downstream needs.
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": m.id, "object": "model", "created": 0, "owned_by": "openai"}
                    for m in self.config.models
                ],
            }
        )

    async def _chat(self, request: Request) -> Response:
        payload = await self._record(request)
        denied = self._auth_error(request)
        if denied is not None:
            return denied
        scripted = await self._next_scripted()
        if scripted is not None:
            return scripted
        resolved = self._resolve(payload, expected_mode="chat")
        if isinstance(resolved, Response):
            return resolved
        model = resolved

        messages = payload.get("messages") if payload else None
        prompt_tokens = _token_estimate(messages)
        reply = self.config.chat_reply
        return JSONResponse(
            {
                "id": f"chatcmpl-fake-{self.request_count}",
                "object": "chat.completion",
                "created": 0,
                "model": model.id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": reply},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": len(reply.split()) or 1,
                    "total_tokens": prompt_tokens + (len(reply.split()) or 1),
                },
            }
        )

    async def _embeddings(self, request: Request) -> Response:
        payload = await self._record(request)
        denied = self._auth_error(request)
        if denied is not None:
            return denied
        scripted = await self._next_scripted()
        if scripted is not None:
            return scripted
        resolved = self._resolve(payload, expected_mode="embedding")
        if isinstance(resolved, Response):
            return resolved
        model = resolved

        raw = payload.get("input") if payload else None
        inputs = [raw] if isinstance(raw, str) else [str(x) for x in cast("list[Any]", raw or [])]
        prompt_tokens = sum(len(t.split()) or 1 for t in inputs)
        return JSONResponse(
            {
                "object": "list",
                "model": model.id,
                "data": [
                    {
                        "object": "embedding",
                        "index": i,
                        # Constant, non-zero, and the configured width. A zero
                        # vector would make a cosine ranking meaningless in a
                        # way no assertion downstream would notice.
                        "embedding": [0.5] * model.embedding_dim,
                    }
                    for i in range(len(inputs))
                ],
                "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
            }
        )

    async def _rerank(self, request: Request) -> Response:
        """Cohere-shaped rerank. Only a `mode="rerank"` model is accepted.

        No model in :data:`DEFAULT_MODELS` has that mode, on purpose: the
        default gateway here reproduces the deployment Aleph runs against,
        where `/v1/rerank` exists and nothing can serve it. A test that wants a
        working reranker adds one.
        """
        payload = await self._record(request)
        denied = self._auth_error(request)
        if denied is not None:
            return denied
        scripted = await self._next_scripted()
        if scripted is not None:
            return scripted
        resolved = self._resolve(payload, expected_mode="rerank")
        if isinstance(resolved, Response):
            return resolved
        model = resolved

        raw = (payload or {}).get("documents")
        documents = [str(d) for d in cast("list[Any]", raw or [])]
        query = str((payload or {}).get("query") or "")
        top_n = (payload or {}).get("top_n")
        limit = top_n if isinstance(top_n, int) and top_n > 0 else len(documents)
        scored = sorted(
            ((index, _overlap_score(query, document)) for index, document in enumerate(documents)),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return JSONResponse(
            {
                "id": f"rerank-fake-{self.request_count}",
                "model": model.id,
                "results": [
                    {"index": index, "relevance_score": score} for index, score in scored[:limit]
                ],
                # Cohere bills rerank in search units and reports no tokens.
                # Emitted in that shape so the cost path is exercised against
                # the response a real reranker sends, not a convenient one.
                "meta": {"billed_units": {"search_units": 1}},
            }
        )

    async def _not_found(self, request: Request) -> Response:
        await self._record(request)
        return JSONResponse(
            {"error": {"message": f"Not Found: {request.url.path}", "code": "404"}},
            status_code=404,
        )

    # ---- internals ---------------------------------------------------------

    def _enter(self) -> None:
        self._counters.in_flight += 1
        self._counters.peak_in_flight = max(self._counters.peak_in_flight, self._counters.in_flight)

    async def _record(self, request: Request) -> dict[str, Any] | None:
        body: dict[str, Any] | None = None
        if request.method == "POST":
            try:
                parsed: object = await request.json()
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                body = cast("dict[str, Any]", parsed)
        self._counters.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.url.path,
                authorization=request.headers.get("authorization"),
                body=body,
            )
        )
        return body

    def _auth_error(self, request: Request) -> Response | None:
        if not self.config.require_auth:
            return None
        if request.headers.get("authorization") == f"Bearer {self.config.api_key}":
            return None
        return JSONResponse(
            {
                "error": {
                    "message": "Invalid proxy server token passed.",
                    "type": "auth_error",
                    "code": "401",
                }
            },
            status_code=401,
        )

    async def _next_scripted(self) -> Response | None:
        if not self._script:
            return None
        head = self._script[0]
        if head.times <= 1:
            self._script.popleft()
        else:
            self._script[0] = replace(head, times=head.times - 1)
        if head.delay_s:
            await asyncio.sleep(head.delay_s)
        headers = {"Retry-After": head.retry_after} if head.retry_after is not None else None
        return JSONResponse(head.body or {}, status_code=head.status, headers=headers)

    def _resolve(
        self, payload: dict[str, Any] | None, *, expected_mode: str
    ) -> FakeModel | Response:
        """The requested model, or the 4xx a real gateway would answer with."""
        requested = (payload or {}).get("model")
        name = requested if isinstance(requested, str) else ""
        model = self._models.get(name)
        if model is None:
            # The dead-RAG defect in one line: the profile named a model this
            # gateway does not serve, and every call 400'd.
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            f"Invalid model name passed in model={name!r}. "
                            "Pass a model that this gateway serves."
                        ),
                        "type": "invalid_request_error",
                        "code": "400",
                    }
                },
                status_code=400,
            )
        if model.invoke_error is not None:
            return JSONResponse(
                {
                    "error": {
                        "message": model.invoke_error,
                        "type": "invalid_request_error",
                        "code": str(model.invoke_status),
                    }
                },
                status_code=model.invoke_status,
            )
        if model.mode != expected_mode:
            return JSONResponse(
                {
                    "error": {
                        "message": (
                            f"model={model.id} has mode={model.mode!r} and cannot serve a "
                            f"{expected_mode} request"
                        ),
                        "type": "invalid_request_error",
                        "code": "400",
                    }
                },
                status_code=400,
            )
        return model

    def _info_row(self, model: FakeModel) -> dict[str, Any]:
        info: dict[str, Any] = {
            "mode": model.mode,
            "max_input_tokens": model.max_input_tokens,
            "max_output_tokens": model.max_output_tokens,
            "supports_vision": model.supports_vision,
            "supports_function_calling": model.supports_function_calling,
            "supports_reasoning": model.supports_reasoning,
            "supports_prompt_caching": model.supports_prompt_caching,
        }
        if self.config.report_rates:
            # Emitted as JSON numbers because that is what LiteLLM emits, and
            # the parser's `Decimal(str(v))` route is exactly what has to be
            # exercised — money must not inherit binary float error.
            for key, raw in (
                ("input_cost_per_token", model.input_per_token),
                ("output_cost_per_token", model.output_per_token),
                ("cache_read_input_token_cost", model.cache_read_per_token),
                ("cache_creation_input_token_cost", model.cache_write_per_token),
            ):
                if raw is not None:
                    info[key] = float(raw)
        row: dict[str, Any] = {"model_name": model.id, "model_info": info}
        if model.upstream is not None:
            row["litellm_params"] = {"model": model.upstream}
        return row


def _overlap_score(query: str, document: str) -> float:
    """Word overlap, in [0, 1]. Crude, deterministic, and INPUT-DERIVED.

    Constant scores would make every ordering assertion downstream vacuous —
    the caller would pass whether or not it read the response — which is the
    same reasoning as `_token_estimate` below.
    """
    terms = {w for w in query.lower().split() if w}
    if not terms:
        return 0.0
    words = {w for w in document.lower().split() if w}
    return len(terms & words) / len(terms)


def _token_estimate(messages: object) -> int:
    """Deterministic, input-derived token count.

    A constant would make every cost assertion in every downstream test
    identical, which hides an arithmetic error in the cost path — the exact
    class of defect that let unpriced calls read $0.
    """
    if not isinstance(messages, list):
        return 1
    total = 0
    for m in cast("list[object]", messages):
        if isinstance(m, dict):
            content = cast("dict[str, object]", m).get("content")
            if isinstance(content, str):
                total += len(content.split())
    return total or 1

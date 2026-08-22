"""Agent LLM cost-attribution callback (Wave 6 C1).

Closes the rule-#5 gap for the Live Deep Agent: its `ChatOpenAI` calls
(in `copilot_agent.build_assistant_deep_agent`) bypass `LiteLLMClient`, so
without this callback they write NO `ModelCall` + `CostLedgerEvent` rows.

`AgentCostCallbackHandler` is a LangChain `AsyncCallbackHandler` attached
**only** to the agent's `ChatOpenAI`. It mirrors `CostWriter.record_call`
(the same write `LiteLLMClient._record_call` performs) for the agent's own
turns. It is NEVER attached to `LiteLLMClient`, so the LiteLLMClient path
(wiki compile, reviewers, page-selection, the `read_wiki` retrieval) is not
double-counted.

The handler must never crash the agent: every DB write is wrapped, and a
failure (or an unresolvable project scope, or missing token usage) is logged
and skipped — the agent turn proceeds regardless.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from langchain_core.callbacks import AsyncCallbackHandler

from aleph_api.chat_runs import current_model_call_scope
from aleph_core.time import utcnow

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from langchain_core.outputs import LLMResult

    from aleph_models.pricing import PricingTable

logger = logging.getLogger(__name__)

# Cap on in-flight run_id -> project_id entries. A CANCELLED run (client
# disconnect -> asyncio.CancelledError, a BaseException) fires neither
# on_llm_end nor on_llm_error, so its entry never gets popped. To prevent
# unbounded growth under churn, _remember_project evicts oldest entries
# (FIFO via OrderedDict) once this cap is exceeded.
_MAX_PENDING = 2048


def _project_id_from_metadata(metadata: dict[str, Any] | None) -> UUID | None:
    """Resolve the project scope from a run's `metadata`.

    LangChain forwards the run's `RunnableConfig.metadata` (which LangGraph
    merges `configurable` into) to the callback's start hooks. The project
    rides one of:
      - an explicit `projectId` / `project_id` key, or
      - the project-prefixed thread id `proj:<uuid>:<thread>` that the Node
        CopilotRuntime formats (surfaced here as `thread_id`).
    Mirrors `copilot_agent._project_id_from_config`'s parsing.
    """
    if not metadata:
        return None
    raw = metadata.get("projectId") or metadata.get("project_id")
    if not raw:
        thread_id = metadata.get("thread_id") or ""
        if isinstance(thread_id, str) and thread_id.startswith("proj:"):
            parts = thread_id.split(":", 2)
            if len(parts) >= 2:
                raw = parts[1]
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _agent_run_id_from_metadata(metadata: dict[str, Any] | None) -> UUID | None:
    """Best-effort agent-run id from a run's metadata (when the turn minted one)."""
    if not metadata:
        return None
    raw = metadata.get("agent_run_id") or metadata.get("agentRunId")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _as_int(value: Any) -> int:
    """Coerce a possibly-None / non-int token count to a non-negative int."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _extract_usage(response: LLMResult) -> tuple[int, int, int, int] | None:
    """Pull (input_tokens, cached_tokens, completion_tokens, cache_write_tokens).

    Handles both shapes defensively:
      1. `response.generations[0][0].message.usage_metadata` — the LangChain
         standard shape (`input_tokens`, `output_tokens`,
         `input_token_details.cache_read`). This is what ChatOpenAI populates
         from the gateway's OpenAI-style `usage` block.
      2. `response.llm_output["token_usage"]` — the legacy OpenAI shape
         (`prompt_tokens`, `completion_tokens`).
    Returns None when no usage is present (caller then skips the write).
    """
    # Shape 1: usage_metadata on the generation message.
    try:
        generations: Any = getattr(response, "generations", None) or []
        for batch in generations:
            for gen in batch:
                message = getattr(gen, "message", None)
                usage_raw = getattr(message, "usage_metadata", None)
                if usage_raw:
                    usage: dict[str, Any] = dict(usage_raw)
                    input_tokens = _as_int(usage.get("input_tokens"))
                    completion_tokens = _as_int(usage.get("output_tokens"))
                    details: dict[str, Any] = dict(usage.get("input_token_details") or {})
                    cached = _as_int(details.get("cache_read"))
                    # Cache WRITES were never read here, and `PricingTable`
                    # models them at a 1.25x premium — so every first call in a
                    # cached conversation under-reported, which is the opposite
                    # failure to the one caching exists to fix and is invisible
                    # because the reported number simply gets smaller.
                    cache_write = _as_int(details.get("cache_creation", details.get("cache_write")))
                    if input_tokens or completion_tokens:
                        return input_tokens, cached, completion_tokens, cache_write
    except Exception:
        logger.debug("usage_metadata extraction failed", exc_info=True)

    # Shape 2: llm_output.token_usage (legacy OpenAI).
    try:
        llm_output: dict[str, Any] = dict(getattr(response, "llm_output", None) or {})
        token_usage: dict[str, Any] = dict(
            llm_output.get("token_usage") or llm_output.get("usage") or {}
        )
        if token_usage:
            input_tokens = _as_int(
                token_usage.get("prompt_tokens", token_usage.get("input_tokens"))
            )
            completion_tokens = _as_int(
                token_usage.get("completion_tokens", token_usage.get("output_tokens"))
            )
            details = dict(token_usage.get("prompt_tokens_details") or {})
            cached = _as_int(details.get("cached_tokens"))
            cache_write = _as_int(
                details.get("cache_creation_tokens", details.get("cache_write_tokens"))
            )
            if input_tokens or completion_tokens:
                return input_tokens, cached, completion_tokens, cache_write
    except Exception:
        logger.debug("llm_output token_usage extraction failed", exc_info=True)

    return None


class AgentCostCallbackHandler(AsyncCallbackHandler):
    """Writes a ModelCall + CostLedgerEvent for the agent model's own turns.

    Constructed once (the agent model is built once at startup), but resolves
    `project_id` per-run from the start hook's `metadata` and stores it keyed
    by `run_id` so `on_llm_end` can use it. `session_maker` may be passed in or
    read lazily from `copilot_agent._runtime` at call time (preferred, to match
    the file's lazy-runtime pattern — the graph is built before `bind_runtime`).
    """

    def __init__(
        self,
        *,
        session_maker: Any | None = None,
        pricing: PricingTable | None = None,
        model: str,
        purpose: str = "assistant.turn",
    ) -> None:
        super().__init__()
        self._session_maker = session_maker
        self._pricing = pricing
        self._model = model
        self._purpose = purpose
        # run_id -> (project_id, agent_run_id, start_monotonic) resolved at start,
        # consumed at end. Ordered so the oldest entries can be evicted when the
        # size cap is exceeded (a CANCELLED run never reaches
        # on_llm_end/on_llm_error to pop itself).
        self._pending: OrderedDict[UUID, tuple[UUID, UUID | None, float]] = OrderedDict()
        #: Log the unbound case once per handler rather than once per call: an
        #: error repeated on every model call trains people to filter it out.
        self._warned_unbound = False

    # ---- runtime resolution (lazy) ----------------------------------------

    def _resolve_session_maker(self) -> Any | None:
        if self._session_maker is not None:
            return self._session_maker
        # Lazy read from the bound runtime, mirroring the tools in copilot_agent.
        try:
            from aleph_api.copilot_agent import get_runtime

            return get_runtime().get("session_maker")
        except Exception:
            return None

    def _resolve_pricing(self) -> PricingTable:
        """The pricing table the kernel bound, or an empty one — and say which.

        This used to *fabricate* an empty `PricingTable()` and MEMOISE it. Two
        consequences, and the second is the one that made it invisible:

        1. Every model call on the agent path recorded `pricing_source="unknown"`
           — 100% of assistant traffic, which is the most expensive traffic in
           the system. CLAUDE.md's stated commitment is "an unpriced call is
           never a silent $0"; a permanent "unknown" is worse than a silent $0,
           because it looks like a recorded fact.
        2. Memoising the empty table meant that even once the gateway came up
           and discovery filled the real one, this handler kept its empty copy
           for the life of the process.

        So: read the bound table lazily, the same way `_resolve_session_maker`
        does, and do NOT cache the miss. `refresh_pricing` merges into the bound
        table in place, so holding the object (rather than a copy) is what makes
        newly discovered rates reach the cost path with no restart.

        The two failures are also logged apart. "No pricing table is bound" is a
        wiring bug; "this model is absent from the table" is a discovery gap.
        They were indistinguishable, and they need different fixes.
        """
        if self._pricing is not None:
            return self._pricing

        try:
            from aleph_api.copilot_agent import get_runtime

            bound = get_runtime().get("pricing")
        except Exception:
            bound = None

        if bound is not None:
            # Cache only a HIT. Caching the miss is what froze the empty table.
            self._pricing = bound
            return bound

        from aleph_models.pricing import PricingTable

        if not self._warned_unbound:
            self._warned_unbound = True
            _log.error(
                "agent.cost.no_pricing_table_bound",
                purpose=self._purpose,
                detail=(
                    "the kernel's PRICING capability is not on the agent runtime, so "
                    "every agent ModelCall will record pricing_source='unknown'. This "
                    "is a wiring bug, not a discovery gap."
                ),
            )
        return PricingTable()

    # ---- lifecycle hooks ---------------------------------------------------

    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: Any,
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._remember_project(run_id, metadata)

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._remember_project(run_id, metadata)

    def _remember_project(self, run_id: UUID, metadata: dict[str, Any] | None) -> None:
        project_id = _project_id_from_metadata(metadata)
        if project_id is not None:
            # `metadata` FIRST for the project (it carries the thread id), and
            # the task-local scope first for the run id — because LangChain does
            # not merge `configurable` into callback metadata, so
            # `metadata["agent_run_id"]` is never populated by anything. That is
            # why this column was unconditionally NULL: not a bug in the reader,
            # a channel that does not carry the key. `AlephAgentMiddleware`
            # publishes the scope around the call.
            scope = current_model_call_scope()
            agent_run_id = (
                scope.agent_run_id if scope and scope.agent_run_id is not None else None
            ) or _agent_run_id_from_metadata(metadata)
            self._pending[run_id] = (project_id, agent_run_id, time.monotonic())
            # Evict oldest entries left behind by CANCELLED runs (which fire
            # neither on_llm_end nor on_llm_error), keeping _pending bounded.
            while len(self._pending) > _MAX_PENDING:
                self._pending.popitem(last=False)

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        entry = self._pending.pop(run_id, None)
        if entry is None:
            # No resolvable project scope — skip writing, but log WHY (rule #5:
            # never a silent drop) without crashing the agent turn.
            logger.warning(
                "agent cost attribution skipped: no project scope resolved (run_id=%s, purpose=%s)",
                run_id,
                self._purpose,
            )
            return
        project_id, agent_run_id, t0 = entry
        usage = _extract_usage(response)
        if usage is None:
            # A row with zero tokens and `pricing_source=unknown`, not silence.
            #
            # This used to `return`, so a response carrying no usage block — a
            # provider that omits it, or `stream_usage` unset — produced no
            # record at all. An absent row and a free call are indistinguishable
            # once they are both nothing, and the whole point of the ledger is
            # that spend is never invisible. Zero tokens with a stated reason is
            # a claim someone can check; an absence is not.
            logger.warning(
                "agent model call reported no token usage (run_id=%s, purpose=%s) — "
                "is stream_usage=True set? recorded as unpriced, not as absent",
                run_id,
                self._purpose,
            )
            usage = (0, 0, 0, 0)
        input_tokens, cached_tokens, completion_tokens, cache_write_tokens = usage
        latency_ms = max(int((time.monotonic() - t0) * 1000), 0)
        try:
            await self._write(
                project_id=project_id,
                agent_run_id=agent_run_id,
                input_tokens=input_tokens,
                cached_tokens=cached_tokens,
                completion_tokens=completion_tokens,
                cache_write_tokens=cache_write_tokens,
                latency_ms=latency_ms,
            )
        except Exception:
            logger.warning(
                "agent cost attribution write failed (run_id=%s, purpose=%s)",
                run_id,
                self._purpose,
                exc_info=True,
            )

    async def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """A call that failed after burning tokens is still spend.

        This used to pop the pending entry and record NOTHING. A provider that
        streams a partial response and then errors has already billed for what
        it produced, and dropping it made a failing model look free — the exact
        direction of error that hides a problem instead of surfacing it.

        The token counts are usually unavailable on the error path, so the row
        is written with what is known: the project, the run, the model, and
        `pricing_source=unknown` with a reason. A row saying "this call
        happened and we could not price it" is checkable; silence is not.
        """
        entry = self._pending.pop(run_id, None)
        if entry is None:
            return
        project_id, agent_run_id, t0 = entry
        try:
            await self._write(
                project_id=project_id,
                agent_run_id=agent_run_id,
                input_tokens=0,
                cached_tokens=0,
                completion_tokens=0,
                latency_ms=max(int((time.monotonic() - t0) * 1000), 0),
                failure=f"{type(error).__name__}: {error}",
            )
        except Exception:
            logger.warning(
                "agent cost attribution write failed on the error path (run_id=%s, purpose=%s)",
                run_id,
                self._purpose,
                exc_info=True,
            )

    # ---- the write ---------------------------------------------------------

    async def _write(
        self,
        *,
        project_id: UUID,
        agent_run_id: UUID | None,
        input_tokens: int,
        cached_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        cache_write_tokens: int = 0,
        failure: str | None = None,
    ) -> None:
        session_maker = self._resolve_session_maker()
        if session_maker is None:
            logger.warning(
                "agent cost attribution skipped: no session_maker bound (purpose=%s)",
                self._purpose,
            )
            return
        pricing = self._resolve_pricing()
        # The model the request actually named. `self._model` is resolved when
        # the graph is built, so switching a project's profile mid-session
        # mislabelled every row after it — a recorded fact that is quietly wrong
        # is worse than an absent one.
        scope = current_model_call_scope()
        model = (scope.model if scope and scope.model else None) or self._model
        priced = pricing.breakdown(
            model=model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        cost, savings = priced.cost_usd, priced.cache_savings_usd
        if failure is not None:
            logger.warning(
                "agent model call failed after starting (model=%s purpose=%s): %s — "
                "recorded as an unpriced call, not dropped",
                model,
                self._purpose,
                failure,
            )
        elif not priced.priced:
            # Same failure as the transport path: recording $0 for a model we
            # cannot price makes a broken pricing table look like a cheap day.
            logger.error(
                "agent model call could not be priced (model=%s purpose=%s); "
                "recorded with pricing_source=unknown, not as free",
                model,
                self._purpose,
            )
        trace_id: str | None
        try:
            from aleph_observability.tracing import current_trace_id

            trace_id = current_trace_id()
        except Exception:
            trace_id = None

        from aleph_db.repos.cost import CostWriter
        from aleph_observability.tracing import start_span

        with start_span(
            "assistant.cost.record",
            **{
                "aleph.project_id": str(project_id),
                "aleph.purpose": self._purpose,
                "aleph.model": model,
            },
        ):
            async with session_maker() as session:
                writer = CostWriter(session)
                await writer.record_call(
                    project_id=project_id,
                    agent_run_id=agent_run_id,
                    capability="chat",
                    model=model,
                    # The purpose carries the failure marker so `select ... where
                    # purpose like '%.failed'` finds every call that was billed
                    # for nothing. Otherwise a failed call is indistinguishable
                    # from a zero-token success.
                    purpose=f"{self._purpose}.failed" if failure else self._purpose,
                    input_tokens=input_tokens,
                    cached_tokens=cached_tokens,
                    completion_tokens=completion_tokens,
                    # `cache_write_tokens` was a column nothing wrote. Cache
                    # writes bill at a premium, so omitting them made every
                    # first call in a cached conversation under-report.
                    cache_write_tokens=cache_write_tokens,
                    cost_usd=cost,
                    cache_savings_usd=savings,
                    pricing_source=priced.source,
                    input_rate_usd=priced.input_rate_usd,
                    output_rate_usd=priced.output_rate_usd,
                    latency_ms=latency_ms,
                    trace_id=trace_id,
                    timestamp=utcnow(),
                )
                await session.commit()

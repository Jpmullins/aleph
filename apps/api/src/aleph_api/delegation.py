"""The `AsyncSubAgent` specs that point deepagents at Aleph's own Agent Protocol.

`docs/decisions.md` D17, phase A. The routes exist (`routes/agent_protocol.py`);
this is what makes the supervisor use them. Passing these into
`create_deep_agent(subagents=[…])` mounts `AsyncSubAgentMiddleware` and gives the
supervisor `start_async_task`, `check_async_task`, `update_async_task`,
`cancel_async_task` and `list_async_tasks`.

**The credential cannot be baked into the spec, and that is the whole reason
this module is not four lines.**

`agent_resolution_signature` keys the compiled-graph cache on
`(endpoint, bindings, api_key)` — NOT on the caller. One graph is therefore
shared by every user of a project, so a token minted at graph-build time would
attribute every delegation any of them started to whoever happened to build the
graph. That is the same defect class as the agent scope taken from a
client-supplied thread id, and it would be invisible: the ledger would simply be
wrong about who asked.

So the token is resolved **per turn**, from a `ContextVar` the request sets, and
the spec is a lazy `Mapping` rather than a dict. `_resolve_headers` calls
`spec.get("headers")`, so anything Mapping-shaped works, and `create_deep_agent`
demultiplexes on `"graph_id" in spec`, which a Mapping also answers.

**Tokens are bucketed, and that is not premature.** `_ClientCache` in the
middleware keys its httpx clients on `(url, frozenset(headers.items()))` and
never evicts. A freshly minted token per turn would mean a new client per turn,
retained for the life of the process — an unbounded leak in an API that runs for
weeks. Bucketing the mint to a 30-minute window makes the header value stable
within that window, so the cache holds at most two entries per (project, user)
per hour, and every token still has at least 30 minutes of its hour left when it
is handed out.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from aleph_security.agent_token import mint_agent_token

if TYPE_CHECKING:
    from aleph_api.settings import Settings

#: How long a minted delegation token is reused before a fresh one is minted.
#: Half the token's own one-hour ceiling, so the oldest token handed out still
#: has thirty minutes left.
_BUCKET_SECONDS: Final[int] = 1800

#: (project_id, user_id) for the turn in flight. Set by the agent entrypoint.
#: A ContextVar rather than a module global because turns run concurrently in one
#: process and a global would hand one caller's credential to another.
_TURN_SCOPE: ContextVar[tuple[UUID, UUID] | None] = ContextVar(
    "aleph_delegation_scope", default=None
)

#: (project, user, bucket) -> token. Bounded by eviction of stale buckets.
_TOKENS: dict[tuple[UUID, UUID, int], str] = {}


def set_turn_scope(*, project_id: UUID, user_id: UUID) -> None:
    """Bind this turn's delegation credential scope. Call once per request."""
    _TURN_SCOPE.set((project_id, user_id))


def current_turn_scope() -> tuple[UUID, UUID] | None:
    return _TURN_SCOPE.get()


def _delegation_token(secret: str, project_id: UUID, user_id: UUID) -> str:
    """A project- and caller-scoped token, reused within its bucket."""
    bucket = int(time.time()) // _BUCKET_SECONDS
    key = (project_id, user_id, bucket)
    cached = _TOKENS.get(key)
    if cached is not None:
        return cached
    # Drop every bucket but the current one. Without this the map grows by one
    # entry per (project, user) per half hour for the life of the process.
    for stale in [k for k in _TOKENS if k[2] != bucket]:
        del _TOKENS[stale]
    token = mint_agent_token(
        secret=secret,
        user_id=user_id,
        project_id=project_id,
        # The delegation is not itself an agent run; the run it starts is created
        # by the route. A fresh id here keeps the claim well-formed without
        # naming a run that does not exist.
        agent_run_id=project_id,
        actor_kind="aleph_agent",
        correlation_id=f"delegation:{bucket}",
    )
    _TOKENS[key] = token
    return token


class _LazySpec(Mapping[str, Any]):
    """An `AsyncSubAgent` whose headers are resolved when they are read.

    Everything except `headers` is fixed at construction. `headers` is a
    function of the turn, for the reason in the module docstring.
    """

    __slots__ = ("_base", "_secret")

    def __init__(self, base: dict[str, Any], *, secret: str) -> None:
        self._base = base
        self._secret = secret

    def _headers(self) -> dict[str, str]:
        scope = _TURN_SCOPE.get()
        if scope is None:
            # No turn scope means nothing legitimate is delegating. Returning an
            # empty header set makes the route answer 403 rather than letting an
            # unscoped call through, which is the safe direction.
            return {}
        project_id, user_id = scope
        return {"Authorization": f"Bearer {_delegation_token(self._secret, project_id, user_id)}"}

    def __getitem__(self, key: str) -> Any:
        if key == "headers":
            return self._headers()
        return self._base[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._base
        yield "headers"

    def __len__(self) -> int:
        return len(self._base) + 1

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<AsyncSubAgent {self._base.get('name')!r} -> {self._base.get('url')!r}>"


#: What each delegatable subagent is FOR, in the supervisor's terms.
#:
#: The supervisor picks by reading these, so they are action-oriented and say
#: when to reach for one — a description like "helps with stuff" produces a
#: supervisor that delegates at random.
_DESCRIPTIONS: Final[dict[str, str]] = {
    "retriever": (
        "Reads the project's wiki and corpus deeply and returns a distilled, cited "
        "answer. Use for questions needing many passages read in full."
    ),
    "researcher": (
        "Runs the full research loop — plan, search, ingest, reflect, compose — over "
        "external sources. Use for open questions the current corpus cannot answer. "
        "Long-running."
    ),
    "wiki_builder": (
        "Ingests a source URL or document, or promotes a note into a draft wiki page. "
        "Use when new material should become durable knowledge."
    ),
    "viz_builder": (
        "Builds charts, reports, decks and exports. Use when the answer should be an "
        "artifact rather than a message."
    ),
    "reviewer": (
        "Reviews a wiki page for contradictions, weak sources and coverage gaps. Use "
        "before treating a page as settled."
    ),
}


def async_subagent_specs(settings: Settings) -> list[Mapping[str, Any]]:
    """One `AsyncSubAgent` per delegatable subagent, pointed at this API.

    Returns Mappings, not dicts: `create_deep_agent` checks `"graph_id" in spec`
    and `_resolve_headers` calls `spec.get("headers")`, both of which a Mapping
    answers, and the laziness is what keeps one user's credential out of a graph
    shared with another's.
    """
    from aleph_api.subagents import DELEGATABLE

    # `getattr`, and an empty list rather than an exception, for two reasons.
    #
    # Several tests build Settings as a SimpleNamespace carrying only the fields
    # they exercise, and an optional capability should not force every one of
    # them to be updated.
    #
    # More importantly it is the right PRODUCT behaviour: a deployment with no
    # loopback URL or no token secret cannot delegate, and the honest response is
    # that the delegation tools do not appear. `AsyncSubAgentMiddleware` is only
    # mounted when at least one async spec exists, so returning nothing here
    # leaves a supervisor that never offers a capability it cannot deliver —
    # rather than one that offers five tools and fails on the first call.
    base = getattr(settings, "aleph_api_internal_url", "") or ""
    secret = getattr(settings, "aleph_agent_token_secret", "") or ""
    if not base or not secret:
        return []

    base_url = base.rstrip("/") + "/v1/agent-protocol"
    return [
        _LazySpec(
            {
                "name": name,
                "description": _DESCRIPTIONS.get(name, f"The {name} subagent."),
                "graph_id": name,
                "url": base_url,
            },
            secret=secret,
        )
        for name in sorted(DELEGATABLE)
    ]

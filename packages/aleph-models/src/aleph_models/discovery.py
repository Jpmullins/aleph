"""Ask the gateway what it serves, instead of guessing.

Aleph ships no list of models. It asks the gateway, and derives everything
downstream from the answer: the options offered in Settings, the default
capability bindings for a new project, and — critically — the prices used to
cost every call.

The alternative was a table of model names and rates committed to the repo.
That is what shipped, and it failed in both directions at once against the
first real gateway it met:

* **Every name was wrong.** The table bound ``claude-sonnet-4-6``; the gateway
  serves ``bedrock-claude-sonnet-4-6``. Every call returns 400.
* **Every rate was wrong.** The table priced Opus input at $15/MTok. This
  gateway bills $5.50. Even where a name *did* match, cost was over-reported
  by roughly 3x.
* **And an unknown name cost $0**, so the second failure was invisible. A
  dashboard reading $0.00 looks like a quiet day, not a broken pipeline.

A gateway knows its own prices and its own capabilities. Asking it is less
work and more correct, and it means adding a model to the gateway is a config
change rather than a pull request.

Discovery covers what the gateway *advertises*. It cannot know what the
gateway can actually *reach* — this deployment lists a Sonnet whose Bedrock
invocation fails for want of an inference profile. That is what
:func:`probe_model` is for, and why bootstrap probes rather than trusts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import httpx
import structlog

from aleph_core.schemas.model_profile import Capability
from aleph_models.pricing import PricingTable

_log = structlog.get_logger(__name__)

#: Endpoint carrying per-model pricing *and* capability flags. `/v1/models`
#: returns bare ids, and `/model_group/info` omits the cache-token rates that
#: prompt caching makes load-bearing, so neither is sufficient on its own.
_MODEL_INFO_PATH = "/model/info"


def _decimal(v: Any) -> Decimal | None:
    """Gateway JSON numbers → Decimal, via ``str`` to avoid float artifacts.

    ``Decimal(2.65e-06)`` carries the binary representation's error into money.
    ``Decimal(str(2.65e-06))`` does not.
    """
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):  # bool is an int subclass; never a rate
        return None
    if isinstance(v, (int, float, str)):
        try:
            return Decimal(str(v))
        except (ArithmeticError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class DiscoveredModel:
    """One model the gateway advertises, with everything needed to use and bill it."""

    id: str
    mode: str | None
    max_input_tokens: int | None
    max_output_tokens: int | None
    input_per_token: Decimal | None
    output_per_token: Decimal | None
    cache_read_per_token: Decimal | None
    cache_write_per_token: Decimal | None
    supports_vision: bool
    supports_function_calling: bool
    supports_reasoning: bool
    supports_prompt_caching: bool
    providers: tuple[str, ...] = ()

    #: False when the gateway advertises no input rate. Such a model is
    #: listable but must never be bound by default — billing it would record
    #: the same silent $0 this module exists to remove.
    @property
    def is_priced(self) -> bool:
        return self.input_per_token is not None

    @property
    def blended_cost(self) -> Decimal:
        """A single comparable number, weighted toward input.

        Aleph's traffic is prompt-heavy — wiki pages into a selector, chunks
        into an extractor — so ranking on output rate alone would misorder
        models for the light capabilities. 3:1 approximates observed usage and
        only ever decides *ties in tier*, never correctness.
        """
        i = self.input_per_token or Decimal("0")
        o = self.output_per_token or Decimal("0")
        return i * 3 + o


def parse_model_info(payload: Any) -> list[DiscoveredModel]:
    """Parse a LiteLLM ``/model/info`` body. Unparseable entries are skipped.

    Tolerant by design: a gateway that adds a field, or serves one malformed
    row among six good ones, should not take model discovery down with it.
    """
    raw: object = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    rows = cast("list[object]", raw)

    out: list[DiscoveredModel] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        entry = cast("dict[str, object]", row)
        name = entry.get("model_name")
        if not isinstance(name, str) or not name:
            continue

        raw_info = entry.get("model_info")
        info: dict[str, object] = (
            cast("dict[str, object]", raw_info) if isinstance(raw_info, dict) else {}
        )
        raw_params = entry.get("litellm_params")
        providers: tuple[str, ...] = ()
        if isinstance(raw_params, dict):
            upstream = cast("dict[str, object]", raw_params).get("model")
            if isinstance(upstream, str) and "/" in upstream:
                providers = (upstream.split("/", 1)[0],)

        def _int(key: str, src: dict[str, object] = info) -> int | None:
            v = src.get(key)
            return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        def _flag(key: str, src: dict[str, object] = info) -> bool:
            return bool(src.get(key))

        mode = info.get("mode")
        out.append(
            DiscoveredModel(
                id=name,
                mode=mode if isinstance(mode, str) else None,
                max_input_tokens=_int("max_input_tokens"),
                max_output_tokens=_int("max_output_tokens"),
                input_per_token=_decimal(info.get("input_cost_per_token")),
                output_per_token=_decimal(info.get("output_cost_per_token")),
                cache_read_per_token=_decimal(info.get("cache_read_input_token_cost")),
                cache_write_per_token=_decimal(info.get("cache_creation_input_token_cost")),
                supports_vision=_flag("supports_vision"),
                supports_function_calling=_flag("supports_function_calling"),
                supports_reasoning=_flag("supports_reasoning"),
                supports_prompt_caching=_flag("supports_prompt_caching"),
                providers=providers,
            )
        )
    # Stable order so Settings lists and generated defaults are reproducible.
    out.sort(key=lambda m: m.id)
    return out


async def discover_models(
    *,
    base_url: str,
    api_key: str,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 20.0,
) -> list[DiscoveredModel]:
    """GET ``/model/info`` and parse it.

    `client` is injectable so tests can fake the HTTP boundary without faking
    any of the parsing, selection, or pricing that follows it.
    """
    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout_s)
    try:
        resp = await http.get(
            f"{base_url.rstrip('/')}{_MODEL_INFO_PATH}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return parse_model_info(resp.json())
    finally:
        if owned:
            await http.aclose()


async def probe_model(
    *,
    base_url: str,
    api_key: str,
    model: DiscoveredModel,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 45.0,
) -> str | None:
    """Actually call `model`. Returns ``None`` if it works, else the error.

    A gateway's model list is a statement of configuration, not of
    reachability. This deployment advertises ``bedrock-claude-sonnet-4-6`` and
    then fails at invocation because the underlying Bedrock model needs an
    inference-profile ARN. Binding it by default would have produced a system
    that passes every startup check and fails on first use.
    """
    owned = client is None
    http = client or httpx.AsyncClient(timeout=timeout_s)
    url = f"{base_url.rstrip('/')}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        if model.mode == "embedding":
            resp = await http.post(
                f"{url}/v1/embeddings",
                headers=headers,
                json={"model": model.id, "input": ["ping"]},
            )
        else:
            resp = await http.post(
                f"{url}/v1/chat/completions",
                headers=headers,
                json={
                    "model": model.id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 4,
                },
            )
        if resp.status_code >= 400:
            return f"HTTP {resp.status_code}: {resp.text[:300]}"
        body: object = resp.json()
        if isinstance(body, dict):
            err = cast("dict[str, object]", body).get("error")
            if err:
                return str(err)[:300]
        return None
    except (httpx.HTTPError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"[:300]
    finally:
        if owned:
            await http.aclose()


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityPolicy:
    """What a capability *requires*, and which way to lean when choosing.

    Requirements are hard filters derived from gateway metadata — a model
    without ``supports_vision`` cannot serve the vision capability, whatever
    it costs. Tier only orders the survivors.
    """

    mode: str
    #: "heavy" prefers the most capable survivor, "light" the cheapest.
    tier: str
    needs_vision: bool = False
    needs_function_calling: bool = False
    #: Minimum context window. Page selection loads the whole wiki index into
    #: one prompt, so an 8k model is disqualified regardless of price — this is
    #: the filter that stops "cheapest" from selecting a model that cannot do
    #: the job.
    min_input_tokens: int = 0


#: Capability → what it needs. Deliberately expressed as requirements over
#: discovered metadata rather than as model names, so a gateway swap changes
#: nothing here.
CAPABILITY_POLICIES: dict[Capability, CapabilityPolicy] = {
    Capability.SYNTHESIS: CapabilityPolicy(
        mode="chat", tier="heavy", needs_function_calling=True, min_input_tokens=100_000
    ),
    Capability.JUDGE: CapabilityPolicy(mode="chat", tier="heavy", min_input_tokens=100_000),
    Capability.CODE: CapabilityPolicy(mode="chat", tier="heavy", needs_function_calling=True),
    Capability.VISION: CapabilityPolicy(mode="chat", tier="heavy", needs_vision=True),
    Capability.PAGE_SELECTION: CapabilityPolicy(
        mode="chat", tier="light", min_input_tokens=100_000
    ),
    Capability.EXTRACTION: CapabilityPolicy(mode="chat", tier="light", needs_function_calling=True),
    Capability.CLASSIFICATION: CapabilityPolicy(mode="chat", tier="light"),
    Capability.EMBEDDING: CapabilityPolicy(mode="embedding", tier="light"),
    Capability.RERANK: CapabilityPolicy(mode="rerank", tier="light"),
}


def candidates_for(
    policy: CapabilityPolicy, models: list[DiscoveredModel]
) -> list[DiscoveredModel]:
    """Models that satisfy `policy`, best first.

    Ranking uses capability signals before cost. Within a tier the ordering is
    total and deterministic — model id breaks every remaining tie — so the same
    gateway always yields the same defaults.
    """
    ok = [
        m
        for m in models
        if m.mode == policy.mode
        # An unpriced model is never selected automatically: binding one would
        # reintroduce silent $0 accounting through the back door.
        and m.is_priced
        and (not policy.needs_vision or m.supports_vision)
        and (not policy.needs_function_calling or m.supports_function_calling)
        and (m.max_input_tokens or 0) >= policy.min_input_tokens
    ]
    # Model id breaks every remaining tie, so the same gateway always yields
    # the same defaults. Heavy prefers the LAST id, which on every version
    # scheme in practice ("...opus-4.6" vs "...opus-4.7") means the newer
    # model; light prefers the first. Both passes rely on `sort` being stable.
    ok.sort(key=lambda m: m.id, reverse=policy.tier == "heavy")
    if policy.tier == "heavy":
        # Reasoning first, then larger context, then more expensive — cost is a
        # weak proxy for capability, so it only ever breaks ties the explicit
        # signals leave open.
        ok.sort(
            key=lambda m: (
                not m.supports_reasoning,
                -(m.max_input_tokens or 0),
                -m.blended_cost,
            )
        )
    else:
        ok.sort(key=lambda m: (m.blended_cost, -(m.max_input_tokens or 0)))
    return ok


def binding_for(model: DiscoveredModel, fallback: DiscoveredModel | None = None) -> dict[str, Any]:
    """A `ModelBindingIn`-shaped dict carrying the gateway's own numbers."""
    out: dict[str, Any] = {
        "model": model.id,
        "provider": "litellm",
        "max_input_tokens": model.max_input_tokens or 200_000,
        "cost_per_input_token_usd": model.input_per_token or Decimal("0"),
        "cost_per_output_token_usd": model.output_per_token or Decimal("0"),
    }
    if fallback is not None:
        out["fallback"] = binding_for(fallback)
    return out


def select_default_bindings(
    models: list[DiscoveredModel],
    *,
    unreachable: frozenset[str] = frozenset(),
) -> dict[str, dict[str, Any]]:
    """Capability → binding, chosen from what the gateway actually serves.

    Capabilities with no qualifying model are left **unbound** rather than
    pointed at something that cannot do the job. An unbound capability raises a
    clear error at resolution time; a wrongly-bound one fails in the middle of
    a research run, or worse, silently produces bad output.

    `unreachable` drops models that are advertised but fail on invocation.
    """
    usable = [m for m in models if m.id not in unreachable]
    bindings: dict[str, dict[str, Any]] = {}
    for capability, policy in CAPABILITY_POLICIES.items():
        ranked = candidates_for(policy, usable)
        if not ranked:
            continue
        bindings[capability.value] = binding_for(ranked[0], ranked[1] if len(ranked) > 1 else None)
    return bindings


def unbound_capabilities(bindings: dict[str, Any]) -> list[Capability]:
    """Capabilities this gateway cannot serve — reported, never papered over."""
    return [c for c in Capability if c.value not in bindings]


def capabilities_for(model: DiscoveredModel) -> list[str]:
    """Capabilities `model` is *eligible* for, by the same rules defaults use.

    Sent to the UI so the Settings picker offers only models that can do the
    job, without reimplementing the policy in TypeScript — a second copy would
    drift, and the drift would show up as a binding that passes validation and
    fails at runtime.
    """
    return [c.value for c, p in CAPABILITY_POLICIES.items() if model in candidates_for(p, [model])]


class GatewayCatalog:
    """Cached view of what the gateway serves, and the prices it charges.

    Holds a TTL so Settings does not re-query on every keystroke, and refreshes
    a `PricingTable` **in place** — the same object `LiteLLMClient` was
    constructed with — so newly discovered rates reach the cost path without
    rebuilding the client.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        ttl_s: float = 300.0,
    ) -> None:
        self._base_url = base_url
        self._api_key = api_key
        self._client = client
        self._ttl_s = ttl_s
        self._models: list[DiscoveredModel] = []
        self._fetched_at: float | None = None

    @property
    def cached(self) -> list[DiscoveredModel]:
        return list(self._models)

    def _is_fresh(self) -> bool:
        return self._fetched_at is not None and (time.monotonic() - self._fetched_at) < self._ttl_s

    async def models(self, *, force: bool = False) -> list[DiscoveredModel]:
        if not force and self._is_fresh():
            return list(self._models)
        self._models = await discover_models(
            base_url=self._base_url, api_key=self._api_key, client=self._client
        )
        self._fetched_at = time.monotonic()
        return list(self._models)

    async def refresh_pricing(self, pricing: PricingTable, *, force: bool = False) -> int:
        """Fold discovered rates into `pricing`. Returns the count priced.

        Failure is logged and swallowed: a gateway that is briefly unreachable
        must not stop the API from booting. The cost of that choice is that
        calls made before the first successful discovery record
        `pricing_source="unknown"` — visible in the ledger, which is the point.
        """
        try:
            models = await self.models(force=force)
        except (httpx.HTTPError, ValueError) as exc:
            _log.error(
                "gateway.discovery_failed",
                base_url=self._base_url,
                error=str(exc),
                impact="model calls will record pricing_source=unknown until discovery succeeds",
            )
            return 0
        discovered = PricingTable.from_discovery(models)
        pricing.merge(discovered)
        _log.info("gateway.discovered", models=len(models), priced=len(discovered.models()))
        return len(discovered.models())

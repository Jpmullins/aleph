"""Operator-supplied facts for gateways that will not describe themselves.

`/model/info` is the honest source: the gateway reports its own modes, context
windows, capability flags and per-token rates. When Aleph can read it, nothing
in this module is used.

But that endpoint is an **admin** route. A LiteLLM *virtual* key — the kind an
application is actually given — is restricted to ``llm_api_routes`` and gets:

    403 "Virtual key is not allowed to call this route.
         Only allowed to call routes: ['llm_api_routes']"

All such a key can read is `/v1/models`, which returns ids and nothing else.
Discovery therefore knows *that* `claude-opus-4-7` exists and nothing about what
it can do or what it costs.

This file fills that gap, under three rules that keep it from becoming the
committed price table that caused the original problem:

1. **It never overrides the gateway.** Hints apply only to fields discovery
   left unset, so a deployment with an admin key silently gets better data.
2. **It is labelled.** Rates from here produce ``pricing_source="static"`` on
   every `ModelCall`, distinct from ``"gateway"``. An auditor can tell which
   numbers were asserted and which were reported.
3. **It does not invent.** Capability facts (mode, context window, tool/vision
   support) are properties of the *model* and are safe to state. **Rates are
   properties of the deployment** and are only listed for commercial models
   with a published list price — and even then they are an estimate to verify
   against a bill. Self-hosted and open-weight models are left unpriced on
   purpose, so binding one is loudly `unknown` rather than quietly wrong.

Point `ALEPH_MODEL_HINTS_PATH` at your own JSON to override the shipped file.
"""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aleph_models.discovery import DiscoveredModel

_log = structlog.get_logger(__name__)

DEFAULT_HINTS_PATH = pathlib.Path(__file__).with_name("model_hints.json")


def _decimal_or_none(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, str)):
        try:
            return Decimal(str(v))
        except (ArithmeticError, ValueError):
            return None
    return None


def load_hints(path: str | pathlib.Path | None = None) -> dict[str, dict[str, Any]]:
    """Read the hints file. A broken or missing file is never fatal.

    Discovery must keep working without hints — the result is simply a system
    that knows less and says so, which is the failure mode this codebase
    prefers.
    """
    target = pathlib.Path(path or os.environ.get("ALEPH_MODEL_HINTS_PATH") or DEFAULT_HINTS_PATH)
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _log.warning("model_hints.unreadable", path=str(target), error=str(exc))
        return {}
    if not isinstance(raw, dict):
        return {}
    models = cast("dict[str, Any]", raw).get("models")
    if not isinstance(models, dict):
        return {}
    return {
        k: cast("dict[str, Any]", v)
        for k, v in cast("dict[str, Any]", models).items()
        if isinstance(v, dict)
    }


def _hint_for(model_id: str, hints: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Exact id first, then the longest matching ``prefix*`` key.

    Longest-wins so `claude-haiku-4-5-20251001` can be covered by a dated entry
    without a shorter `claude-*` rule stealing it.
    """
    if model_id in hints:
        return hints[model_id]
    best: tuple[int, dict[str, Any]] | None = None
    for key, value in hints.items():
        if key.endswith("*") and model_id.startswith(key[:-1]):
            n = len(key)
            if best is None or n > best[0]:
                best = (n, value)
    return best[1] if best else None


def apply_hints(
    models: list[DiscoveredModel], hints: dict[str, dict[str, Any]] | None = None
) -> list[DiscoveredModel]:
    """Fill unset fields from hints. Gateway-reported values always win."""
    table = load_hints() if hints is None else hints
    if not table:
        return models

    out: list[DiscoveredModel] = []
    for m in models:
        hint = _hint_for(m.id, table)
        if hint is None:
            out.append(m)
            continue
        changes: dict[str, Any] = {}
        if m.mode is None and isinstance(hint.get("mode"), str):
            changes["mode"] = hint["mode"]
        if m.max_input_tokens is None and isinstance(hint.get("max_input_tokens"), int):
            changes["max_input_tokens"] = hint["max_input_tokens"]
        if m.max_output_tokens is None and isinstance(hint.get("max_output_tokens"), int):
            changes["max_output_tokens"] = hint["max_output_tokens"]
        for field, key in (
            ("input_per_token", "input_per_token"),
            ("output_per_token", "output_per_token"),
            ("cache_read_per_token", "cache_read_per_token"),
            ("cache_write_per_token", "cache_write_per_token"),
        ):
            if getattr(m, field) is None:
                v = _decimal_or_none(hint.get(key))
                if v is not None:
                    changes[field] = v
                    if field == "input_per_token":
                        # An asserted rate must never be labelled as reported.
                        changes["rates_source"] = "hints"
        for flag in (
            "supports_vision",
            "supports_function_calling",
            "supports_reasoning",
            "supports_prompt_caching",
        ):
            if not getattr(m, flag) and bool(hint.get(flag)):
                changes[flag] = True
        if changes:
            # `metadata_available` becomes true only once the model actually has
            # a mode to filter on; a hint that supplied only a price still
            # leaves it unbindable, which is the safe direction.
            if changes.get("mode") or m.mode:
                changes["metadata_available"] = True
            out.append(replace(m, **changes))
        else:
            out.append(m)
    return out

"""Gateway base-URL shaping, for the two clients with opposite conventions.

One configured value feeds clients that disagree about who owns the `/v1`
segment:

* :class:`~aleph_models.client.LiteLLMClient` and
  :mod:`aleph_models.discovery` build `{base}/v1/chat/completions` and
  `{base}/v1/models` themselves, so they need a **bare origin**.
* `ChatOpenAI` (openai-python) appends only `/chat/completions`, so its base
  must **already carry** `/v1` — that is what `copilot_agent._openai_base_url`
  is for, and this is its inverse.

**Both input forms have to work, because both are what people are told to
type.** Every vLLM, Ollama and LM Studio quickstart prints a base URL ending in
`/v1`, since that is what the OpenAI SDK wants. A LiteLLM gateway is normally
configured without it. A user pasting the form their server's own documentation
gave them is not making a mistake.

Measured 2026-08-28: a vLLM endpoint stored as `http://host:8003/v1` produced
requests to `http://host:8003/v1/v1/models`, a 404 the probe reported as the
gateway being unreachable — while the same server answered `/v1/models`
perfectly well from the same container.
"""

from __future__ import annotations

_V1 = "/v1"


def gateway_origin(base_url: str) -> str:
    """The bare origin, for clients that append `/v1/...` themselves.

    Idempotent, and tolerant of a trailing slash: `http://h:8003/v1/`,
    `http://h:8003/v1` and `http://h:8003` all return `http://h:8003`.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith(_V1):
        trimmed = trimmed[: -len(_V1)].rstrip("/")
    return trimmed


def openai_base_url(base_url: str) -> str:
    """The `/v1`-carrying form, for the OpenAI SDK. Inverse of :func:`gateway_origin`."""
    return f"{gateway_origin(base_url)}{_V1}"

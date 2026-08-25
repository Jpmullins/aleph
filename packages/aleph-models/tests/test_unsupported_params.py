"""A gateway is the authority on what its models accept; Aleph is not.

`claude-opus-4-7` on this instance reaches Bedrock, which answers
`400 - "temperature is deprecated for this model"`. 1,072 model profiles bind
that model and 15 call sites pass a hardcoded temperature, so every one of
those combinations was a hard failure — `assistant.compose` among them, which
is why the retrieval debug route returned 500.

Fixing the 15 call sites would be the wrong shape: the next model to drop a
sampling knob breaks them all again. The knowledge belongs where the gateway's
answer arrives.

The dangerous half of this feature is the guess. Dropping the wrong key would
silently change what was asked for, and the request would SUCCEED, so nothing
would report it. Most of what follows tests the refusals.
"""

from __future__ import annotations

import httpx
import pytest

from aleph_models.client import (
    _DROPPABLE_PARAMS,
    _unsupported_param,
    _without_known_unsupported,
    reset_unsupported_params,
)

URL = "https://gateway.example/v1/chat/completions"
BEDROCK_400 = (
    '{"error":{"message":"litellm.BadRequestError: BedrockException - '
    '{\\"message\\":\\"The model returned the following errors: `temperature` '
    'is deprecated for this model.\\"}. Received Model Group=claude-opus-4-7",'
    '"code":"400"}}'
)


@pytest.fixture(autouse=True)
def _clean() -> None:
    reset_unsupported_params()


def _err(status: int, body: str) -> httpx.HTTPStatusError:
    resp = httpx.Response(status_code=status, text=body, request=httpx.Request("POST", URL))
    return httpx.HTTPStatusError("boom", request=resp.request, response=resp)


def test_the_real_bedrock_message_is_understood() -> None:
    """The exact body this instance returned, not a paraphrase of it."""
    payload = {"model": "claude-opus-4-7", "temperature": 0.2, "messages": []}
    assert _unsupported_param(_err(400, BEDROCK_400), payload) == "temperature"


def test_a_parameter_the_request_never_sent_is_not_dropped() -> None:
    """The check that stops a stray word in an error message removing something.

    Same complaint, but this caller passed no temperature. There is nothing to
    drop, so the error must propagate rather than be retried identically.
    """
    payload = {"model": "claude-opus-4-7", "messages": []}
    assert _unsupported_param(_err(400, BEDROCK_400), payload) is None


def test_a_non_droppable_parameter_is_never_dropped() -> None:
    """`messages` and `response_format` change the CONTRACT of the request."""
    body = '{"error":{"message":"Unsupported parameter: `response_format`"}}'
    payload = {"model": "m", "response_format": {"type": "json_object"}, "messages": []}
    assert _unsupported_param(_err(400, body), payload) is None
    assert "response_format" not in _DROPPABLE_PARAMS
    assert "messages" not in _DROPPABLE_PARAMS
    assert "model" not in _DROPPABLE_PARAMS


def test_an_unrelated_400_is_not_treated_as_a_parameter_problem() -> None:
    body = '{"error":{"message":"context length exceeded: 210000 > 200000"}}'
    payload = {"model": "m", "temperature": 0.2, "messages": []}
    assert _unsupported_param(_err(400, body), payload) is None


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 503])
def test_only_400_is_considered(status: int) -> None:
    """A 429 must stay retryable and a 403 must stay an auth problem."""
    payload = {"model": "m", "temperature": 0.2, "messages": []}
    assert _unsupported_param(_err(status, BEDROCK_400), payload) is None


def test_what_is_learned_is_applied_to_later_calls() -> None:
    base, model = "https://gw", "claude-opus-4-7"
    payload = {"model": model, "temperature": 0.2, "top_p": 0.9, "messages": []}
    assert _without_known_unsupported(base, model, payload) == payload

    from aleph_models.client import _remember_unsupported

    _remember_unsupported(base, model, "temperature")
    stripped = _without_known_unsupported(base, model, payload)
    assert "temperature" not in stripped
    assert stripped["top_p"] == 0.9, "only the refused knob is removed"
    assert stripped["messages"] == []


def test_learning_is_scoped_to_the_model_and_the_gateway() -> None:
    """A different model, or the same model on another gateway, is unaffected."""
    from aleph_models.client import _remember_unsupported

    payload = {"model": "other", "temperature": 0.2}
    _remember_unsupported("https://gw", "claude-opus-4-7", "temperature")
    assert _without_known_unsupported("https://gw", "other", payload) == payload
    assert _without_known_unsupported("https://elsewhere", "claude-opus-4-7", payload) == payload


def test_reset_actually_forgets() -> None:
    from aleph_models.client import _remember_unsupported

    _remember_unsupported("https://gw", "m", "temperature")
    reset_unsupported_params()
    payload = {"model": "m", "temperature": 0.2}
    assert _without_known_unsupported("https://gw", "m", payload) == payload

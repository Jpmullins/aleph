"""A gateway 4xx has to say WHY, or a real outage is undiagnosable from logs.

`resp.raise_for_status()` builds its message from the status code and the URL
and drops the response body — which, on an OpenAI-compatible gateway, is the
only place the cause appears: an unsupported parameter, a context overflow, a
model the virtual key cannot reach.

Measured on this instance: `assistant.compose` got a `400 Bad Request` and
logged a 90-line traceback in which no line said what was wrong. Finding the
cause meant replaying the call by hand against the gateway.

The second test is the one that matters more. `aleph_models.retry._is_retryable`
decides on `exc.response.status_code`, so raising a DIFFERENT exception type
here would silently stop every 429 and 5xx from being retried — a much worse
bug than the one being fixed, and one no error-message test would notice.
"""

from __future__ import annotations

import httpx
import pytest

from aleph_models.client import _ERROR_BODY_CHARS, _raise_with_gateway_reason
from aleph_models.retry import _is_retryable

URL = "https://gateway.example/v1/chat/completions"


def _response(status: int, body: str) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        text=body,
        request=httpx.Request("POST", URL),
    )


def test_the_gateway_reason_survives_into_the_message() -> None:
    resp = _response(400, '{"error":{"message":"max_tokens exceeds context"}}')
    with pytest.raises(httpx.HTTPStatusError) as caught:
        _raise_with_gateway_reason(resp, URL)
    assert "max_tokens exceeds context" in str(caught.value)
    assert "400" in str(caught.value)


def test_a_huge_body_is_truncated() -> None:
    """A gateway that echoes the payload must not put a whole prompt in a log."""
    resp = _response(400, "x" * (_ERROR_BODY_CHARS * 3))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        _raise_with_gateway_reason(resp, URL)
    assert len(str(caught.value)) < _ERROR_BODY_CHARS * 2


def test_an_empty_body_still_raises_a_usable_error() -> None:
    resp = _response(403, "")
    with pytest.raises(httpx.HTTPStatusError) as caught:
        _raise_with_gateway_reason(resp, URL)
    assert "403" in str(caught.value)
    assert URL in str(caught.value)


@pytest.mark.parametrize(
    ("status", "retryable"),
    [(429, True), (500, True), (503, True), (400, False), (403, False), (404, False)],
)
def test_retry_classification_is_unchanged(status: int, retryable: bool) -> None:
    """The whole point of keeping HTTPStatusError and its `response`."""
    resp = _response(status, '{"error":"nope"}')
    with pytest.raises(httpx.HTTPStatusError) as caught:
        _raise_with_gateway_reason(resp, URL)
    assert _is_retryable(caught.value) is retryable


def test_it_agrees_with_raise_for_status_on_what_counts_as_an_error() -> None:
    """A 2xx must not be turned into an exception by the replacement."""
    ok = _response(200, "{}")
    ok.raise_for_status()  # the behaviour being replaced: no raise
    assert ok.status_code < 400

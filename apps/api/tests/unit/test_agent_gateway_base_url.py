"""The agent's gateway URL must carry `/v1`; LiteLLMClient's must not.

One env var feeds two clients with opposite conventions. Getting it wrong sends
every agent turn to `{base}/chat/completions`, which an OpenAI-compatible server
404s — and the user sees "Run ended without emitting a terminal event", which
names neither the URL nor the status code.
"""

from __future__ import annotations

import pytest

from aleph_api.copilot_agent import _openai_base_url


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # The bare origin LiteLLMClient wants — the agent must not inherit it as-is.
        ("http://host.docker.internal:8010", "http://host.docker.internal:8010/v1"),
        ("https://gateway.example.com", "https://gateway.example.com/v1"),
        # Trailing slashes are an operator typo, not a different endpoint.
        ("http://localhost:8010/", "http://localhost:8010/v1"),
        # Idempotent: an already-versioned base is left alone rather than doubled
        # into `/v1/v1`, which fails exactly as obscurely as the missing one.
        ("http://localhost:8010/v1", "http://localhost:8010/v1"),
        ("http://localhost:8010/v1/", "http://localhost:8010/v1"),
    ],
)
def test_agent_base_url_carries_exactly_one_version_segment(configured: str, expected: str) -> None:
    assert _openai_base_url(configured) == expected


def test_agent_base_url_is_not_the_raw_setting() -> None:
    """The regression itself: passing the setting through unchanged is the bug."""
    raw = "http://host.docker.internal:8010"
    assert _openai_base_url(raw) != raw

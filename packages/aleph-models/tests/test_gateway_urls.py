"""One configured value, two clients that disagree about who owns `/v1`.

`LiteLLMClient` and `discovery` build `{base}/v1/...` themselves, so they need a
bare origin. `ChatOpenAI` appends only `/chat/completions`, so its base must
already carry `/v1`.

**Both input forms have to work, because both are what people are told to type.**
Every vLLM, Ollama and LM Studio quickstart prints a base URL ending in `/v1` —
that is what the OpenAI SDK wants. A LiteLLM gateway is normally configured
without it. A user pasting the form their own server's documentation gave them is
not making a mistake, and Aleph documents itself as pointing at "any
OpenAI-compatible endpoint".

Measured 2026-08-28: a vLLM endpoint stored as `http://host:8003/v1` produced
requests to `http://host:8003/v1/v1/models`, a 404 the probe reported as the
gateway being unreachable — while the same server answered `/v1/models` from the
same container.
"""

from __future__ import annotations

import pytest

from aleph_models.urls import gateway_origin, openai_base_url

BOTH_FORMS = [
    "http://192.168.1.158:8003/v1",
    "http://192.168.1.158:8003/v1/",
    "http://192.168.1.158:8003",
    "http://192.168.1.158:8003/",
]


@pytest.mark.parametrize("configured", BOTH_FORMS)
def test_every_form_a_user_might_paste_yields_one_origin(configured: str) -> None:
    assert gateway_origin(configured) == "http://192.168.1.158:8003"


@pytest.mark.parametrize("configured", BOTH_FORMS)
def test_and_exactly_one_v1_for_the_sdk(configured: str) -> None:
    assert openai_base_url(configured) == "http://192.168.1.158:8003/v1"


def test_a_gateway_configured_without_v1_is_unchanged() -> None:
    """The LiteLLM convention must keep working — this fix must not trade one
    server's form for another's."""
    assert gateway_origin("https://gateway.example.com") == "https://gateway.example.com"
    assert openai_base_url("https://gateway.example.com") == "https://gateway.example.com/v1"


def test_both_helpers_are_idempotent() -> None:
    """Applied twice by two layers, the result must not drift."""
    for u in BOTH_FORMS:
        assert gateway_origin(gateway_origin(u)) == gateway_origin(u)
        assert openai_base_url(openai_base_url(u)) == openai_base_url(u)


def test_a_path_that_merely_ends_in_v1_ish_is_not_stripped() -> None:
    """`/v1` is a path SEGMENT. A host or path that happens to end in those
    characters is not the segment, and stripping it would break a real URL."""
    assert gateway_origin("https://api.example.com/gw/v10") == "https://api.example.com/gw/v10"
    assert gateway_origin("https://myv1.example.com") == "https://myv1.example.com"


def test_the_two_helpers_are_inverses() -> None:
    for u in [*BOTH_FORMS, "https://gateway.example.com"]:
        assert gateway_origin(openai_base_url(u)) == gateway_origin(u)

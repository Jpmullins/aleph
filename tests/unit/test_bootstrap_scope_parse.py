"""The bootstrap scope parser tolerates non-bare-JSON LLM responses.

Bedrock/Anthropic via the gateway does not reliably honor json_object mode, so
the scope step must recover JSON from fenced or prose-prefixed output.
"""

from __future__ import annotations

from aleph_workers.jobs.bootstrap import _loads_lenient


def test_bare_json() -> None:
    out = _loads_lenient('{"overview_md": "hi", "seed_topics": ["A", "B"]}')
    assert out["overview_md"] == "hi"
    assert out["seed_topics"] == ["A", "B"]


def test_fenced_json() -> None:
    text = '```json\n{"overview_md": "hi", "seed_topics": ["A"]}\n```'
    assert _loads_lenient(text)["seed_topics"] == ["A"]


def test_prose_prefixed_json() -> None:
    text = 'Sure! Here is the JSON:\n\n{"overview_md": "x", "seed_topics": []}'
    assert _loads_lenient(text)["overview_md"] == "x"


def test_pure_prose_returns_empty() -> None:
    assert _loads_lenient("I cannot produce JSON for this.") == {}


def test_empty_returns_empty() -> None:
    assert _loads_lenient("") == {}
    assert _loads_lenient("   ") == {}


def test_non_object_returns_empty() -> None:
    # A bare JSON array is not the object we want.
    assert _loads_lenient("[1, 2, 3]") == {}

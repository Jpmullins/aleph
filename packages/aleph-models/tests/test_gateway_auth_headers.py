"""A blank key must produce NO header, not an empty one.

`f"Bearer {key}"` with an empty key is the literal `"Bearer "`, and h11 refuses
to put that on the wire:

    LocalProtocolError: Illegal header value b'Bearer '

It raises in the client BEFORE the request is sent, so the endpoint never sees
it and the error names a header rather than the thing the user was doing.

Measured 2026-08-28 against a vLLM server on a LAN: reachable from the API
container, serving its model list, and Aleph's probe recorded
`last_probe_ok = false` with exactly that message. Every keyless
OpenAI-compatible server is in this class — vLLM, Ollama, llama.cpp, LM Studio —
so this meant Aleph could not talk to any of them, while CLAUDE.md says Aleph
"ships no gateway: point `LITELLM_BASE_URL` at any OpenAI-compatible endpoint".
The one gateway anyone had tested against required a key, which is why it lived.
"""

from __future__ import annotations

import httpx
import pytest

from aleph_models.auth import (
    PLACEHOLDER_API_KEY,
    api_key_or_placeholder,
    gateway_auth_headers,
)


@pytest.mark.parametrize("blank", ["", None, "   ", "\t", "\n"])
def test_a_blank_key_produces_no_header_at_all(blank: str | None) -> None:
    """Whitespace counts as absent: a field a user tabbed through holds `" "`,
    and `"Bearer  "` fails exactly the same way as `"Bearer "`."""
    assert gateway_auth_headers(blank) == {}


def test_a_real_key_is_passed_through() -> None:
    assert gateway_auth_headers("sk-abc") == {"Authorization": "Bearer sk-abc"}


def test_a_key_with_surrounding_whitespace_is_trimmed() -> None:
    """A pasted key often carries a trailing newline, and `Bearer sk-abc\\n` is
    also an illegal header value."""
    assert gateway_auth_headers("  sk-abc\n") == {"Authorization": "Bearer sk-abc"}


async def test_the_old_shape_really_is_rejected_and_the_new_one_gets_through() -> None:
    """The regression, demonstrated against a real socket rather than described.

    `httpx.Headers` does NOT validate at construction, and the refusal does not
    happen before connecting either — h11 rejects the value while SERIALISING
    the request onto an established connection. So proving it needs a server,
    and a hermetic one rather than the LAN box that surfaced the bug.

    Reproduced against that box first, for the record:
      `Bearer ` -> LocalProtocolError: Illegal header value b'Bearer '
      no header -> HTTP 200
      `Bearer EMPTY` -> HTTP 200
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Quiet(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            with pytest.raises(httpx.LocalProtocolError, match=r"[Ii]llegal header value"):
                await client.get(url, headers={"Authorization": "Bearer "})

            # What we emit for a keyless endpoint reaches the server.
            assert (await client.get(url, headers=gateway_auth_headers(""))).status_code == 200
            # And a real key still round-trips.
            got = await client.get(url, headers=gateway_auth_headers("sk-1"))
            assert got.status_code == 200
    finally:
        server.shutdown()
        server.server_close()


def test_the_sdk_placeholder_is_used_only_when_there_is_no_key() -> None:
    """The OpenAI SDK refuses to construct without a key, so a keyless endpoint
    needs a placeholder. `EMPTY` is the one vLLM's own docs use, so such servers
    already ignore it."""
    assert api_key_or_placeholder("") == PLACEHOLDER_API_KEY
    assert api_key_or_placeholder(None) == PLACEHOLDER_API_KEY
    assert api_key_or_placeholder("  ") == PLACEHOLDER_API_KEY
    assert api_key_or_placeholder("sk-real") == "sk-real"


def test_no_module_in_aleph_models_interpolates_bearer_directly() -> None:
    """Six call sites drifted apart once already — two in `discovery`, three in
    `client`, one in `endpoints`. A seventh written by hand would reintroduce
    exactly this bug, so the sweep is the point."""
    import pathlib

    import aleph_models

    root = pathlib.Path(aleph_models.__file__).parent
    offenders = [
        f"{path.name}:{i}"
        for path in root.glob("*.py")
        if path.name != "auth.py"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if "Bearer {" in line
    ]
    assert not offenders, (
        f"raw Bearer interpolation at {offenders}; use "
        f"aleph_models.auth.gateway_auth_headers so a blank key omits the header"
    )

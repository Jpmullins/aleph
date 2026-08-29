"""Gateway auth headers, including the case where there is no key.

**A blank key must mean NO HEADER, not an empty one.** `f"Bearer {key}"` with an
empty key produces the literal `"Bearer "`, which h11 refuses to put on the wire:

    LocalProtocolError: Illegal header value b'Bearer '

That is not a cosmetic failure. It is raised in the client before the request is
sent, so the endpoint never sees it and the error names a header rather than the
thing the user was trying to do. Measured 2026-08-28 against a vLLM server on a
LAN: the endpoint was reachable from the API container and served its model
list, and Aleph's probe reported `last_probe_ok = false` with that message.

**Every keyless OpenAI-compatible server is in this class** — vLLM, Ollama,
llama.cpp, LM Studio all serve without auth by default. So this bug meant Aleph
could not talk to any of them, while `CLAUDE.md` says in its own words that Aleph
"ships no gateway: point `LITELLM_BASE_URL` at any OpenAI-compatible endpoint".
The one gateway anyone had tested against required a key, which is why it
survived.

One helper rather than six `if key:` guards, because the six call sites drifted
apart once already: two in `discovery`, three in `client`, one in `endpoints`.
"""

from __future__ import annotations


def gateway_auth_headers(api_key: str | None) -> dict[str, str]:
    """`{"Authorization": "Bearer <key>"}`, or `{}` when there is no key.

    Whitespace-only counts as absent: a settings field a user tabbed through
    holds `" "`, and `"Bearer  "` fails exactly the same way as `"Bearer "`.
    """
    key = (api_key or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


#: What to hand a client that REQUIRES a key string even when the server wants
#: none. The OpenAI SDK refuses to construct without one, so a keyless endpoint
#: needs a placeholder rather than an empty string — and this is the placeholder
#: vLLM's own documentation uses, so it is the one such servers already ignore.
PLACEHOLDER_API_KEY = "EMPTY"


def api_key_or_placeholder(api_key: str | None) -> str:
    """A key safe to hand the OpenAI SDK, for endpoints that need no auth."""
    return (api_key or "").strip() or PLACEHOLDER_API_KEY

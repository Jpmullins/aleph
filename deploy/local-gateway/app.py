"""A local stand-in for the Insights LiteLLM gateway.

Aleph's `LiteLLMClient` takes a single `base_url` and posts both
`/v1/chat/completions` and `/v1/embeddings` to it. The local vLLM serves chat
only — it has no `/v1/embeddings` route — so pointing Aleph straight at vLLM
makes ingest fail at the embed step, and a source that never chunks is
invisible to *both* legs of retrieval, not just the dense one.

So this process is the gateway: one OpenAI-compatible surface, two backends.

* Chat is **proxied**, byte-for-byte, to vLLM. Streaming is passed through
  unbuffered so token-by-token output and the trailing `usage` chunk (which the
  cost ledger depends on, via `stream_usage=True`) arrive intact.
* Embeddings are served **in-process** by bge-m3 on CPU, because the GPU is
  fully committed to the 27B and evicting it to make room would be a worse
  trade than embedding slowly.

Routing is by model name, not by route: whatever is registered in `_EMBEDDERS`
is served locally and everything else is proxied. That keeps the two backends
from having to agree about anything.

Deliberately not LiteLLM proxy itself: this needs to run inside the vLLM image
(the only local image with an aarch64 torch build), and adding a pip install to
a container start is a worse dependency than 150 lines with no new packages.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import torch
import torch.nn.functional as F
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from transformers import AutoModel, AutoTokenizer

#: Upstream vLLM. Everything not served locally is proxied here verbatim.
UPSTREAM = os.environ.get("GATEWAY_UPSTREAM", "http://127.0.0.1:8003").rstrip("/")

#: Models this process serves itself, name -> HF repo id.
_EMBEDDERS: dict[str, str] = {"bge-m3": os.environ.get("GATEWAY_EMBED_MODEL", "BAAI/bge-m3")}

#: bge-m3's trained context. Longer inputs are truncated rather than refused —
#: a chunker that overshoots should degrade, not fail an entire ingest.
_MAX_TOKENS = 8192

#: CPU inference is the bottleneck here. Serialising it keeps 20 cores on one
#: batch instead of thrashing between several, and bounds peak memory.
_embed_lock = asyncio.Semaphore(1)

_state: dict[str, Any] = {}


def _load_embedder(repo: str) -> tuple[Any, Any]:
    tokenizer = AutoTokenizer.from_pretrained(repo)
    model = AutoModel.from_pretrained(repo, dtype=torch.float32)
    model.eval()
    torch.set_num_threads(max(1, (os.cpu_count() or 4) - 2))
    return tokenizer, model


@asynccontextmanager
async def lifespan(app: FastAPI):
    for name, repo in _EMBEDDERS.items():
        _state[name] = await asyncio.to_thread(_load_embedder, repo)
    _state["http"] = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    yield
    await _state["http"].aclose()


app = FastAPI(title="aleph-local-gateway", lifespan=lifespan)


def _embed_sync(name: str, texts: list[str]) -> tuple[list[list[float]], int]:
    """Dense bge-m3 embeddings: CLS token, L2-normalised — the model's own recipe.

    Returns the vectors and the true prompt-token count, so the cost ledger
    records real usage even though the price of that usage is zero.
    """
    tokenizer, model = _state[name]
    batch = tokenizer(
        texts, padding=True, truncation=True, max_length=_MAX_TOKENS, return_tensors="pt"
    )
    with torch.inference_mode():
        out = model(**batch)
    dense = F.normalize(out.last_hidden_state[:, 0], p=2, dim=-1)
    return dense.tolist(), int(batch["attention_mask"].sum().item())


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "upstream": UPSTREAM, "local": sorted(_EMBEDDERS)}


@app.get("/v1/models")
async def list_models() -> JSONResponse:
    """Union of what vLLM serves and what we serve.

    Aleph's `models` capability probe asserts every bound model appears here, so
    an embedder missing from this list fails boot rather than failing later at
    the first ingest — which is the behaviour we want.
    """
    data: list[dict[str, Any]] = []
    try:
        resp = await _state["http"].get(f"{UPSTREAM}/v1/models", timeout=10.0)
        if resp.status_code == 200:
            data = list(resp.json().get("data", []))
    except httpx.HTTPError:
        # Report our own models even when vLLM is down, so the failure surfaces
        # as "the chat model is missing" rather than an empty, ambiguous list.
        pass
    now = int(time.time())
    data.extend(
        {"id": name, "object": "model", "created": now, "owned_by": "aleph-local"}
        for name in _EMBEDDERS
    )
    return JSONResponse({"object": "list", "data": data})


@app.post("/v1/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    body = await request.json()
    model = body.get("model", "")
    if model not in _EMBEDDERS:
        return JSONResponse(
            {"error": {"message": f"unknown embedding model {model!r}", "type": "invalid_request"}},
            status_code=404,
        )
    raw = body.get("input", [])
    texts = [raw] if isinstance(raw, str) else [str(t) for t in raw]
    if not texts:
        return JSONResponse(
            {"error": {"message": "input is required", "type": "invalid_request"}}, status_code=400
        )

    async with _embed_lock:
        vectors, tokens = await asyncio.to_thread(_embed_sync, model, texts)

    return JSONResponse(
        {
            "object": "list",
            "model": model,
            "data": [
                {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
        }
    )


def _hoist_system_messages(body: bytes) -> bytes:
    """Merge every system message into one at index 0.

    Qwen3's chat template rejects a system message that is not first
    ("System message must be at the beginning"), and LangChain/CopilotKit emits
    a fresh system prompt per turn, so a second turn arrives as
    ``[system, user, user, system, user, user]`` and 400s. Anthropic and OpenAI
    accept that shape, which is why it only appears against a local model.

    Concatenating system parts in order is the semantically safe repair: system
    instructions are additive and order-preserving, so the model sees the same
    instructions it was sent, in the same sequence, at the only position the
    template allows. Non-system messages keep their relative order untouched.

    A no-op when the request is already well-formed, and never raises: a proxy
    that rejects a request it failed to parse is worse than one that forwards it
    and lets the upstream decide.
    """
    try:
        payload = json.loads(body)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return body

        systems = [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]
        others = [m for m in messages if not (isinstance(m, dict) and m.get("role") == "system")]
        if len(systems) < 2 and (not systems or messages[0] is systems[0]):
            return body  # already valid; leave the bytes exactly as sent

        merged = "\n\n".join(
            str(m.get("content")) for m in systems if m.get("content") not in (None, "")
        )
        payload["messages"] = ([{"role": "system", "content": merged}] if merged else []) + others
        print(
            f"[gateway] hoisted {len(systems)} system message(s) to the front "
            f"({len(messages)} -> {len(payload['messages'])} messages)",
            flush=True,
        )
        return json.dumps(payload).encode()
    except (ValueError, TypeError, KeyError):
        return body


@app.api_route("/{path:path}", methods=["GET", "POST"], response_model=None)
async def proxy(path: str, request: Request) -> StreamingResponse | JSONResponse:
    """Everything else goes to vLLM unchanged.

    The upstream response is streamed rather than read, so SSE chat completions
    reach the caller as they are produced instead of arriving in one block at
    the end.
    """
    body = await request.body()
    if path.endswith("chat/completions") and body:
        body = _hoist_system_messages(body)

    headers = {
        k: v
        for k, v in request.headers.items()
        # Drop hop-by-hop and length headers; httpx recomputes them, and a stale
        # content-length on a re-encoded body desynchronises the connection.
        if k.lower() not in {"host", "content-length", "connection", "transfer-encoding"}
    }
    client: httpx.AsyncClient = _state["http"]
    req = client.build_request(
        request.method,
        f"{UPSTREAM}/{path}",
        content=body,
        headers=headers,
        params=request.query_params,
    )
    try:
        upstream = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        return JSONResponse(
            {"error": {"message": f"upstream unreachable: {exc}", "type": "upstream_error"}},
            status_code=502,
        )

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()

    excluded = {"content-length", "content-encoding", "transfer-encoding", "connection"}
    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() not in excluded},
        media_type=upstream.headers.get("content-type"),
    )

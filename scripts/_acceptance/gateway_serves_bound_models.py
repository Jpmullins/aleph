"""H1 — every model bound in a ModelProfile is actually served, and embedding works.

Rule #7 says call sites pass a `Capability` and the profile resolves it to a
model. Nothing checked that the resolved model *exists on the configured
gateway*. That gap is not hypothetical: pointing Aleph at a local vLLM left
every profile bound to `titan-embed-v2`, which that gateway does not serve, and
the resulting failure surfaced only at the first ingest — as a 404 from the
embed step, several layers from the cause.

Two assertions, because listing and working are different claims:

1. **Every distinct model bound in `model_profiles` appears in `/v1/models`.**
   Catches a rebind that names something the gateway does not carry.
2. **The embedding binding actually embeds, at the column's dimension.**
   `document_chunks.embedding` is a fixed-width vector; an embedder that serves
   a different width passes assertion 1 and then fails every write. This is the
   assertion with teeth, because it calls the model rather than reading a list.

Chat is deliberately *not* exercised: a generation is slow and its content is
not checkable without a judge. Assertion 1 covers "the chat model is served",
which is the part that can silently drift.

Exit 0 on success; prints the specific mismatch and exits 1 otherwise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

DB_URL = os.environ.get(
    "ALEPH_TEST_DATABASE_URL",
    "postgresql+asyncpg://aleph:changeme-local@localhost:5432/aleph",
)
BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://localhost:8010").rstrip("/")
API_KEY = os.environ.get("INSIGHTS_LITELLM_API_KEY", "local-no-auth")

#: Must match `aleph_rks.models.EMBEDDING_DIM` / the `Vector(...)` column width.
#: Imported rather than hardcoded so a column change cannot silently diverge.
try:
    from aleph_rks.models import EMBEDDING_DIM
except ImportError:  # pragma: no cover - only when run outside the workspace
    EMBEDDING_DIM = 1024


def _get_json(path: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())


async def _bound_models() -> dict[str, set[str]]:
    """capability -> {model} across every profile row, templates included."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DB_URL)
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "select b.key, b.value->>'model' "
                        "from model_profiles p, jsonb_each(p.bindings_jsonb) b"
                    )
                )
            ).all()
    finally:
        await engine.dispose()

    out: dict[str, set[str]] = {}
    for capability, model in rows:
        if model:
            out.setdefault(str(capability), set()).add(str(model))
    return out


def main() -> int:
    bound = asyncio.run(_bound_models())
    if not bound:
        print("FAIL: no ModelProfile bindings found — nothing to verify")
        return 1

    try:
        served = {str(m["id"]) for m in _get_json("/v1/models").get("data", [])}  # type: ignore[index]
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"FAIL: gateway {BASE_URL} unreachable: {exc}")
        return 1

    wanted = {m for models in bound.values() for m in models}
    missing = sorted(wanted - served)
    if missing:
        where = {m: sorted(c for c, models in bound.items() if m in models) for m in missing}
        print(f"FAIL: bound but not served by {BASE_URL}: {where}; served={sorted(served)}")
        return 1

    embedders = bound.get("embedding", set())
    if not embedders:
        print("FAIL: no 'embedding' capability is bound; ingest cannot chunk")
        return 1

    for model in sorted(embedders):
        try:
            body = _get_json("/v1/embeddings", {"model": model, "input": ["dimension probe"]})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"FAIL: embedding model {model!r} is listed but does not serve: {exc}")
            return 1
        data = body.get("data") or []
        if not data:
            print(f"FAIL: embedding model {model!r} returned no vector")
            return 1
        dim = len(data[0]["embedding"])  # type: ignore[index]
        if dim != EMBEDDING_DIM:
            print(f"FAIL: {model!r} emits dim {dim}, column is {EMBEDDING_DIM}; every write fails")
            return 1

    print(
        f"OK: {len(wanted)} bound model(s) served by {BASE_URL}; "
        f"embedder(s) {sorted(embedders)} emit dim {EMBEDDING_DIM}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

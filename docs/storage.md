# Storage

Asset bytes (uploaded sources, normalized markdown, rendered artifacts, builder outputs) are stored behind an `AssetStore` protocol. The **default backend is the local filesystem** (`fs`); an S3-compatible object store is opt-in. All asset bytes reach the browser through **one authenticated streaming route** — there are no presigned URLs and no browser-reachable object store.

## Backend protocol

`aleph_rks.asset_store` is a `typing.Protocol` with two implementations and a factory:

```python
class AssetStore(Protocol):
    def put_source_asset(*, project_id, source_id, data, mime_type, extension) -> StoredAsset: ...
    def put_normalized_markdown(*, project_id, source_id, version_no, markdown) -> str: ...
    def put_bytes(*, key: str, data: bytes, mime_type: str) -> StoredAsset: ...
    def get(storage_uri: str, *, expected_sha256: str | None = None) -> bytes: ...
    def stream(storage_uri: str, *, chunk_size: int = 1 MiB) -> Iterator[bytes]: ...
```

- `put_bytes` is the generic write used by the render/builder paths (no private-attribute reach-ins).
- `get` verifies SHA-256 (mismatch raises `AssetStoreError`, never silent).
- `stream` is chunked reads for the streaming route (no hash check — the route sends `ETag: <sha256>` from the DB row).

**`FsAssetStore(root)`** — the default. URIs are `fs://<key>`; bytes live at `<root>/<key>`. Writes are atomic (`.tmp/<uuid>` → `os.replace`, parents created). Reads resolve the key against `root` and **reject any path escaping it** (`resolved.is_relative_to(root)`). Reading an `s3://` URI raises (and vice versa) — backends never silently cross.

**`S3AssetStore(endpoint, access_key, secret_key, bucket, secure)`** — for real S3-compatible object stores. Uses the object-store SDK client for I/O; **no presign client, no public endpoint, no `presigned_get_url`**. Bucket ensured at construction.

**`create_asset_store(...)`** — factory selecting by `ALEPH_ASSET_BACKEND` (`fs` default, `s3` opt-in). `backend=s3` with missing s3 config raises at startup — fail fast, no silent `None` fallback. The store always exists.

## Key layout

```
projects/{project_id}/sources/{source_id}/{sha256}.{ext}       ← raw source bytes
projects/{project_id}/normalized/{source_id}/{version_no}.md   ← normalized markdown
projects/{project_id}/renders/{source_id}/{sha12}.{ext}        ← rendered assets
projects/{project_id}/artifacts/{artifact_id}/{version}/...    ← builder outputs
```

`fs://` URIs carry the key verbatim; `s3://<bucket>/<key>` for the s3 backend. `storage_uri` columns are unchanged — switching backends does not migrate rows.

## The streaming route

One authenticated route serves **all** asset bytes to the browser:

```
GET /v1/projects/{project_id}/assets/{asset_kind}/{asset_id}
    asset_kind ∈ {source, rendered, artifact-version}
```

- `source` → `Source` → current `SourceVersion` → `SourceAsset`; `rendered` → `RenderedAsset`; `artifact-version` → `ArtifactVersion`.
- Response: `StreamingResponse` over `AssetStore.stream`, with `Content-Type` (from the row), `Content-Disposition: inline; filename="..."` (sanitized), `ETag: "<sha256>"`, `Cache-Control: private, max-age=3600`, `X-Content-Type-Options: nosniff`, and `If-None-Match` → 304. Unknown / cross-project id → 404.

**Auth posture.** The route sits inside the normal principal boundary: `AuthMiddleware` resolves the principal (401 without a bearer in `oidc` mode — unit-tested in `test_asset_stream_auth.py`), `ProjectScopeDep` requires membership (404 on foreign projects), and read is allowed at VIEWER like every other project GET. A revoked member loses access immediately — presigned URLs could not do that. **No ledger event** — rule 4 covers mutations, and reads are no different (still observable via request logs/OTEL).

**CSP sandbox.** Every non-PDF response sends `Content-Security-Policy: sandbox` — iframe-sandbox semantics enforced server-side, covering direct URL opens too. In `local` auth mode every same-origin request carries ambient auth, so API-origin active content (uploaded HTML/SVG) would otherwise have full API reach; the CSP closes that. PDFs are exempt (Chromium's PDF viewer refuses sandboxed documents and is not page-context script).

## Compose / bootstrap / env

- The object-store services are under `profiles: ["s3"]` — a fresh `bootstrap-local.sh` boots **no object-store container**. `docker compose --profile s3 up` restores it for s3-backend testing.
- `aleph-api` + `aleph-workers` carry `ALEPH_ASSET_BACKEND: fs`, `ALEPH_ASSET_ROOT: /data/assets`, and a shared bind mount `../../data/assets:/data/assets` (api writes, workers read; host-run integration tests see the same files). `/data/` is gitignored; `bootstrap-local.sh` pre-creates `data/assets` owned by `ALEPH_UID:ALEPH_GID`.
- Settings (api + workers): `aleph_asset_backend` (`"fs"`), `aleph_asset_root` (default `data/assets`), `aleph_s3_endpoint/access_key/secret_key/bucket/secure` (required iff backend=s3). The `.env.example` object-store block is opt-in and commented; its root credentials + `ALEPH_S3_BUCKET` configure the container only when the `s3` profile is enabled.

## Security posture

- Asset bytes never leave the principal boundary — no presigned URLs, no direct-to-store browser traffic, no anonymous read window. Project scoping is enforced per request at read time.
- fs backend: path-traversal guard on every read; writes only under `ALEPH_ASSET_ROOT`; atomic replace prevents torn reads.
- s3 backend: credentials live in service env only; nothing browser-facing.
- Agent paths are unchanged: agents reach assets only through typed service methods (rule 3); workers use the store directly as a service, never the route.
- The word `presigned` and the old browser-facing public-endpoint env key do not appear in the codebase.

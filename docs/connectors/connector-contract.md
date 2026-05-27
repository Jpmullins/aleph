# Connector contract

A connector is a typed source-kind plugin. Every connector implements
the `ConnectorBase` Protocol:

```python
class ConnectorBase(Protocol):
    kind: ClassVar[str]
    output_kind: ClassVar[Literal["document", "dataset_rows"]]
    requires_auth: ClassVar[bool]
    metadata_schema: ClassVar[type[BaseModel]]

    async def search(self, ctx: ConnectorContext, query: SearchQuery) -> list[ConnectorResult]: ...
    async def fetch(self, ctx: ConnectorContext, result: ConnectorResult) -> RawPayload: ...
```

## Inc 3 roster

| `kind` | `output_kind` | Auth | Default |
|---|---|---|---|
| `upload` | `document` | none | enabled |
| `tavily` | `document` | API key | enabled |
| `exa` | `document` | API key | enabled |
| `serper` | `document` | API key | enabled |
| `arxiv` | `document` | none | enabled |
| `semantic_scholar` | `document` | optional key | enabled |
| `openalex` | `document` | none (mailto) | enabled |
| `rss` | `document` | none | enabled |
| `huggingface_hub` | `document` | optional | enabled |
| `lens` | `document` | API key | **disabled** until credential provided |

## Callback contract

When AIQ calls a connector's `fetch`, the connector does NOT write to
Postgres or MinIO. It returns a `RawPayload`; AIQ then POSTs
`/internal/v1/aiq/sources` with the payload (base64-encoded). The
aleph-api side calls `register_uploaded_source` — the same path the
Upload connector uses in-process — so every Source goes through the
same ingestion pipeline.

## Credentials

When AIQ runs a connector that needs an API key, AIQ POSTs
`/internal/v1/aiq/credentials/{kind}` with the service token. The
aleph-api side:

1. Verifies the service token.
2. Confirms the connector is enabled for the project's
   `ConnectorBinding` set.
3. Returns the decrypted credential (per-project cipher first;
   deployment-env fallback otherwise).

The API key is NEVER written to a log line, ledger payload, or trace
attribute.

## Adding a new connector

1. Add a `packages/aleph-connectors/src/aleph_connectors/<name>/`
   directory with `register.py` implementing the Protocol.
2. Add a seed row in the next Alembic migration.
3. Add the `nat` registration in the AIQ data_source_registry once
   AIQ exposes the registration hook (vendor/aiq imports trigger this
   at AIQ startup).
4. Add unit tests + a doc page.

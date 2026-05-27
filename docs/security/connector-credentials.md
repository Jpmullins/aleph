# Connector credentials

Per-project encrypted credentials, never returned by any HTTP endpoint
except the AIQ internal callback (and only after service-token
verification).

## Encryption

- **Local / dev:** libsodium SecretBox per project, with key derived
  from `sha256(ALEPH_AGENT_TOKEN_SECRET || project_id)`.
- **Production:** envelope-encrypt with KMS — DEK wrapped by KMS key
  per project; ciphertext as AES-GCM. The `kms_key_arn` column on
  `connector_credentials` records the wrapping key.

`cipher_scheme` ∈ `libsodium-sealed` | `kms-aes-gcm` is stored on the
row so reads pick the right cipher.

## Ledger discipline

Every create / rotate / delete writes a ledger event with the
`connector_kind` in the payload — **never** the plaintext.

## Fallback to deployment defaults

If a project has no project-specific credential for `tavily`, the
service falls back to `os.environ['TAVILY_API_KEY']` (and likewise
for other connectors). This is the §10.4 dev-default behavior.

## API

```
PUT    /v1/projects/{id}/connector-credentials/{kind}   { plaintext }
DELETE /v1/projects/{id}/connector-credentials/{kind}
POST   /v1/projects/{id}/connector-credentials/{kind}/rotate { plaintext }
GET    /v1/projects/{id}/connector-credentials           # owner; names only
```

All owner-only.

## Internal callback

The AIQ container calls `POST /internal/v1/aiq/credentials/{kind}` with
its service token. The route:

1. Verifies the token (issuer, audience, expiry, scope).
2. Looks up the connector by kind; 404 if unknown.
3. Confirms project allowlists this connector via `ConnectorBinding`
   (or `enabled_by_default=True` if no binding row exists).
4. Decrypts via the project-specific cipher; falls back to deployment
   env default if no row exists.
5. Returns `{plaintext}` JSON. The connector inside AIQ uses it for
   one call; it's never persisted.

# Auth

## User auth (OIDC)

`aleph-api` validates incoming `Authorization: Bearer <jwt>` against the
configured OIDC IdP (Keycloak / Auth0 / Cognito). The middleware:

1. Pulls `kid` from the JWT header.
2. Looks up the public key in the cached JWKS (fetches from `ALEPH_AUTH_JWKS_URL` on miss).
3. Verifies signature, issuer (`ALEPH_AUTH_ISSUER`), audience (`ALEPH_AUTH_AUDIENCE`), `exp`, `nbf`.
4. JIT-provisions the `User` row on first sight (ledgered as `user.create`).
5. Attaches a `Principal` to `request.state.principal`.

Subjects with no `User` row trigger one insert; subsequent requests
hit the row by `subject` (the OIDC `sub` claim).

## Project scoping

Routes under `/v1/projects/{project_id}/...` resolve membership via
`project_scope_dep`. **Non-member responses are 404, not 403** — Aleph
never leaks existence across project boundaries.

```python
async def some_route(project_id: ProjectScopeDep, ...):
    require_at_least(principal, project_id, at_least=ProjectRole.OWNER)
    ...
```

## Roles

Three: `owner` > `editor` > `viewer`. `require_at_least` compares ranks.
The first member of a project is its owner (the creator). Only owners
can add/remove members or change roles.

## Agent tokens

Background jobs (Aleph workers and AIQ in Inc 3+) carry **agent tokens**
instead of user JWTs. They are HS256-signed by the API using
`ALEPH_AGENT_TOKEN_SECRET`. Issued via `POST /v1/agent-tokens` (owner-only),
scoped to one `AgentRun`, TTL ≤ 1h.

```python
from aleph_security.agent_token import verify_agent_token
claims = verify_agent_token(token, secret=os.environ["ALEPH_AGENT_TOKEN_SECRET"])
# claims.user_id    — initiating user
# claims.project_id — scope
# claims.agent_run_id
# claims.actor_kind ∈ {"aleph_agent", "aiq_agent"}
# claims.correlation_id
```

The auth middleware accepts either token type. HS256 alg in the JWT
header routes to agent-token verification; RS256 routes to JWT verification.
Token theft requires the gateway secret — rotate per the runbook if leaked.

## Egress

- Aleph workers do not hold Postgres credentials. They use agent tokens
  and call back into `aleph-api` via `ALEPH_API_INTERNAL_URL`.
- Aleph workers do not hold MinIO credentials. Object access in Inc 1+
  goes through a presigned URL minted by the API service.
- AIQ (Inc 3+) is identical: no Postgres, no S3, just an agent token.
- Connector fetches run in egress-restricted worker pods (Inc 3+).
- Playwright render workers (Inc 7+) run with no DB or object-store credentials.

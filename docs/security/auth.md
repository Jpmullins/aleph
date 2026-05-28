# Auth

## Modes

`ALEPH_AUTH_MODE` selects the user-auth path:

| Mode | When | Behavior |
|---|---|---|
| `local` | Local dev (default in `deploy/compose/.env.example`) | JWT path bypassed. Every non-public request is associated with a fixed `dev@aleph.local` user, JIT-provisioned on first sight. No IdP service runs locally. |
| `oidc` | Production / any deployment with a real IdP | Full OIDC JWT verification (see below). |

Agent tokens (HS256, internal) are accepted in **both** modes.

The split exists so local dev is fast and deployment-agnostic: pick the IdP at deploy time (Cognito, Auth0, Authentik, Keycloak, ALB OIDC), set three env vars, switch the mode. No code change.

## User auth (OIDC mode)

`aleph-api` validates incoming `Authorization: Bearer <jwt>` against the
configured OIDC IdP. Any IdP that exposes a JWKS endpoint works —
Cognito, Auth0, Authentik, Keycloak, Microsoft Entra, Google, Okta,
ALB OIDC (the `x-amzn-oidc-data` header is JWT signed by ALB's keys).

The middleware:

1. Pulls `kid` from the JWT header.
2. Looks up the public key in the cached JWKS (fetches from `ALEPH_AUTH_JWKS_URL` on miss).
3. Verifies signature, issuer (`ALEPH_AUTH_ISSUER`), audience (`ALEPH_AUTH_AUDIENCE`), `exp`, `nbf`.
4. JIT-provisions the `User` row on first sight (ledgered as `user.create`).
5. Attaches a `Principal` to `request.state.principal`.

Subjects with no `User` row trigger one insert; subsequent requests
hit the row by `subject` (the OIDC `sub` claim).

### Picking an IdP at deploy time

The three env vars below are all you change between IdPs:

```
ALEPH_AUTH_MODE=oidc
ALEPH_AUTH_ISSUER=<iss claim issued by the IdP>
ALEPH_AUTH_AUDIENCE=<aud claim, default "aleph">
ALEPH_AUTH_JWKS_URL=<https URL to the IdP's JWKS document>
```

Examples:
- AWS Cognito: `ALEPH_AUTH_ISSUER=https://cognito-idp.{region}.amazonaws.com/{userPoolId}` · `ALEPH_AUTH_JWKS_URL={issuer}/.well-known/jwks.json`
- Auth0: `ALEPH_AUTH_ISSUER=https://{tenant}.auth0.com/` (trailing slash matters) · JWKS at `{issuer}.well-known/jwks.json`
- Authentik / Keycloak: realm URL · `{issuer}/protocol/openid-connect/certs`
- ALB OIDC: `ALEPH_AUTH_JWKS_URL=https://public-keys.auth.elb.{region}.amazonaws.com/` and the bearer is the `x-amzn-oidc-data` header.

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

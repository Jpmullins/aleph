# Security

## Auth modes

`ALEPH_AUTH_MODE` selects the user-auth path; the frontend mirrors via `VITE_AUTH_MODE`.

- **`local`** (compose default) — JWT verification is skipped; every non-public request maps to a fixed `dev@aleph.local` principal, JIT-provisioned on first sight. No IdP service runs locally. Agent tokens are still verified.
- **`oidc`** — full JWT/JWKS verification against any OIDC IdP (Cognito, Auth0, Authentik, Keycloak, ALB OIDC) via three env vars: `ALEPH_AUTH_ISSUER`, `ALEPH_AUTH_AUDIENCE`, `ALEPH_AUTH_JWKS_URL`. The OIDC path is dormant in local mode but kept intact, so deploy is a config flip.

Project scoping: `ProjectScopeDep` requires membership and returns **404** (not 403) on foreign projects, so non-members cannot even confirm a project exists.

## Agent tokens

Internal service-to-service auth is short-lived HS256 agent tokens signed with `ALEPH_AGENT_TOKEN_SECRET`. Code that self-calls the API (worker jobs, `copilot_agent`, subagents, `a2ui_handlers`) mints a token scoped to the acting project/agent-run via `mint_agent_token(...)` and it is checked by `verify_agent_token`. There is **no hardcoded `Bearer local-dev` in server code** — that ambient-auth sentinel was removed (it 401'd in oidc mode). The only remaining local-mode sentinel is the documented frontend one in `apps/web/src/lib/auth.ts`. External agents/workers obtain a scoped token by `POST /v1/agent-tokens`.

## ConnectorCredential encryption + Consensus OAuth

`ConnectorCredential` stores third-party secrets encrypted at rest with a per-project **libsodium sealed-box** cipher (a KMS-AES-GCM hook exists for production). Plaintext is decrypted only server-side inside the owning service, never logged, never in ledger payloads, and never returned by any route. All writes go through `ConnectorCredentialService` (ledger `connector_credential.create|update|delete`).

The **Consensus** credential is an OAuth blob (`{client_id, token_endpoint, refresh_token, access_token, access_token_expires_at, status}`) bootstrapped by `scripts/connect-consensus.py` (RFC 9728/8414/7591 discovery + PKCE loopback — requires the user at a browser). Scholar refreshes the access token server-side; a rotated refresh token re-upserts the blob (ledgered, redis-locked per project). An authoritative refresh rejection (HTTP 400/401) yields a queryable `reconnect_required` status, never a 500. The credentials GET route exposes a derived `status` (owner-only; plaintext still never returned).

## code_runner isolation (amended rule 8)

Agent-written code executes **only** in the `aleph-code-runner` service — never in a credentialed process. Its isolation:

- **Network-partitioned.** Its only network is `code-runner-net` (`internal: true` → no NAT/internet), whose only reachable peer is a **dedicated `code-runner-redis`** — NOT the platform Redis (which carries agent tokens as job args, privileged job queues, and the LISTEN/NOTIFY streams). The dedicated bus carries only `run_code_job(code, output_kind, timeout_s)`: no tokens, no privileged jobs, no cross-project data.
- **No credentials.** No `DATABASE_URL` / `ALEPH_S3_*` / `LITELLM_*` / `ALEPH_AGENT_TOKEN_SECRET`; no asset bind mount.
- **Locked down.** `cap_drop: [ALL]`, `read_only: true` rootfs + small tmpfs scratch, `no-new-privileges`, non-root user, `pids_limit`, tight `mem_limit`/`memswap_limit`. The agent-code subprocess is additionally denied sockets (`python -I` + socket guard; best-effort `unshare(NEWNET)`).
- **Worst case.** A full escape yields only CPU/mem within the cgroup caps and the agent's own submitted code. Residual (documented): a raw-ctypes syscall could bypass the Python socket guard and reach `code-runner-redis`, but that bus carries only code-job payloads (no secrets), the rootfs is read-only, and all caps are dropped — so the worst case is disruption of the ephemeral code queue, never token capture or privileged-job injection.

The trusted `aleph-workers` dual-homes onto both networks: it dispatches/awaits code jobs and does the privileged persistence (turning returned bytes into versioned artifacts).

## CSP-sandbox asset serving

The one asset streaming route (`GET /v1/projects/{pid}/assets/{kind}/{id}`, see `storage.md`) sends `Content-Security-Policy: sandbox` on every non-PDF response, so uploaded/compiled HTML/SVG runs with iframe-sandbox semantics enforced server-side even on a direct URL open. Interactive artifact cards (`HtmlDocCard`/`HtmlFrameCard`) render only in `sandbox` iframes (no `allow-same-origin`, no network) whose `src` must be this route — the renderer refuses otherwise. This matters because in `local` mode every same-origin request carries ambient auth.

## Ledger hash-chain

Every mutation writes an `ActionLedgerEvent` in the same transaction (rule 4): hash-chained, append-only, no updates or deletes (Postgres immutability triggers). `verify_project_chain` walks the chain via `prev_event_id` from the chain head (not timestamp order), so a tampered `chain_hash` or broken link fails verification even if rows are timestamp-reordered.

## Deferred: SSE × OIDC token transport (documented gap)

The browser `EventSource` API cannot attach an `Authorization` header, so in **`oidc`** mode the SSE streams (agent-events, surfaces, assistant, `changes`) and the asset streaming route consumed by an `<iframe>` cannot carry a bearer token as written. This is a **known, accepted out-of-scope gap** (GOAL §out-of-scope: "Full OIDC deployment hardening beyond the agent-token fix — SSE token transport for EventSource remains a documented gap"). **`local` mode — the only currently-deployed mode — is unaffected** (no bearer is required). Closing it (a short-lived query-param/cookie token exchange for stream endpoints) is future work.

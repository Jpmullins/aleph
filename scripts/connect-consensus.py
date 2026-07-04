#!/usr/bin/env python3
"""Interactive OAuth bootstrap for the Consensus MCP connector (WP-2).

Walks the MCP authorization spec (2025-06-18) end to end:

  1. RFC 9728 protected-resource discovery against the MCP URL
     (401 ``WWW-Authenticate: ... resource_metadata="..."`` or the
     ``/.well-known/oauth-protected-resource`` fallback).
  2. RFC 8414 authorization-server metadata discovery
     (falling back to ``/.well-known/openid-configuration``).
  3. RFC 7591 dynamic client registration (public client, PKCE).
  4. PKCE S256 authorization-code flow with a 127.0.0.1 loopback
     redirect (opens the browser; requires the user interactively).
  5. Token exchange (RFC 8707 ``resource`` indicator included).
  6. ``PUT /v1/projects/{pid}/connector-credentials/consensus`` on the
     aleph API with the composed credential blob.
  7. Smoke check: ``POST .../scholar/consensus-search``.

Usage:
    uv run python scripts/connect-consensus.py --project <uuid> \
        [--api http://localhost:8000] [--mcp-url https://mcp.consensus.app/mcp] \
        [--timeout 30] [--client-id <id>] [--discover-only]

Secrets are never printed; tokens are masked in all output.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import secrets
import sys
import threading
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, NoReturn, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

CALLBACK_WAIT_SECONDS = 600
SMOKE_QUERY = "does creatine improve cognition"


def step(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def fatal(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


def mask(token: str) -> str:
    """Mask a secret for display: first 4 chars + length only."""
    if len(token) <= 8:
        return f"<secret len={len(token)}>"
    return f"{token[:4]}…<len={len(token)}>"


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _get_json(client: httpx.Client, url: str) -> dict[str, Any] | None:
    """GET a URL; return parsed JSON dict on 200, None on any failure."""
    try:
        resp = client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        body: Any = resp.json()
    except ValueError:
        return None
    return cast("dict[str, Any]", body) if isinstance(body, dict) else None


def _str_list(value: Any) -> list[str]:
    """Coerce a JSON value to a list of strings (empty for anything else)."""
    if not isinstance(value, list):
        return []
    return [str(v) for v in cast("list[Any]", value)]


# ---------------------------------------------------------------------------
# Step a — RFC 9728 protected-resource discovery
# ---------------------------------------------------------------------------


def discover_protected_resource(client: httpx.Client, mcp_url: str) -> dict[str, Any]:
    """Return the OAuth protected-resource metadata for the MCP server."""
    candidates: list[str] = []

    # Preferred path: unauthenticated GET -> 401 + WWW-Authenticate resource_metadata.
    try:
        resp = client.get(mcp_url, headers={"Accept": "application/json, text/event-stream"})
        if resp.status_code == 401:
            www = resp.headers.get("www-authenticate", "")
            match = re.search(r'resource_metadata="([^"]+)"', www)
            if match:
                candidates.append(match.group(1))
            else:
                print(
                    "    401 received but WWW-Authenticate lacks resource_metadata; "
                    "falling back to well-known paths"
                )
        else:
            print(
                f"    unauthenticated GET returned {resp.status_code} (expected 401); "
                "falling back to well-known paths"
            )
    except httpx.HTTPError as exc:
        print(f"    unauthenticated probe failed ({exc}); falling back to well-known paths")

    origin = _origin(mcp_url)
    path = urlsplit(mcp_url).path.rstrip("/")
    # RFC 9728 path-aware form, then the root form.
    if path:
        candidates.append(f"{origin}/.well-known/oauth-protected-resource{path}")
    candidates.append(f"{origin}/.well-known/oauth-protected-resource")

    for url in candidates:
        meta = _get_json(client, url)
        if meta and meta.get("authorization_servers"):
            print(f"    protected-resource metadata: {url}")
            return meta

    fatal(
        "could not discover protected-resource metadata for "
        f"{mcp_url} (tried: {', '.join(candidates) or 'nothing'})"
    )


# ---------------------------------------------------------------------------
# Step b — RFC 8414 authorization-server metadata discovery
# ---------------------------------------------------------------------------


def discover_as_metadata(client: httpx.Client, issuer: str) -> dict[str, Any]:
    """Return RFC 8414 / OIDC discovery metadata for the authorization server."""
    origin = _origin(issuer)
    path = urlsplit(issuer).path.rstrip("/")
    candidates = [
        f"{origin}/.well-known/oauth-authorization-server{path}",
        f"{origin}/.well-known/openid-configuration{path}",
        f"{origin}{path}/.well-known/openid-configuration",
    ]
    # Deduplicate while preserving order (path == "" collapses forms 2 and 3).
    seen: set[str] = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]

    for url in candidates:
        meta = _get_json(client, url)
        if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
            print(f"    AS metadata: {url}")
            return meta

    fatal(
        f"could not discover authorization-server metadata for {issuer} "
        f"(tried: {', '.join(candidates)})"
    )


# ---------------------------------------------------------------------------
# Step c — RFC 7591 dynamic client registration
# ---------------------------------------------------------------------------


def register_client(client: httpx.Client, registration_endpoint: str, redirect_uri: str) -> str:
    """Register a public client; return the issued client_id."""
    payload = {
        "client_name": "aleph-scholar",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    try:
        resp = client.post(registration_endpoint, json=payload)
    except httpx.HTTPError as exc:
        fatal(f"dynamic client registration failed: {exc}")
    if resp.status_code not in (200, 201):
        fatal(f"dynamic client registration rejected ({resp.status_code}): {resp.text[:500]}")
    body: Any = resp.json()
    data: dict[str, Any] = cast("dict[str, Any]", body) if isinstance(body, dict) else {}
    client_id = data.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        fatal("registration response did not include a client_id")
    return client_id


# ---------------------------------------------------------------------------
# Step d — PKCE S256 authorization-code flow (loopback redirect)
# ---------------------------------------------------------------------------


class _LoopbackServer(HTTPServer):
    """HTTPServer carrying the captured authorization response."""

    auth_code: str | None = None
    auth_state: str | None = None
    auth_error: str | None = None
    done: threading.Event

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self.done = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path != "/callback":
            self.send_error(404)
            return
        query = parse_qs(parts.query)
        srv = cast("_LoopbackServer", self.server)
        srv.auth_code = (query.get("code") or [None])[0]
        srv.auth_state = (query.get("state") or [None])[0]
        err = (query.get("error") or [None])[0]
        if err:
            desc = (query.get("error_description") or [""])[0]
            srv.auth_error = f"{err}: {desc}" if desc else err
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            b"<html><body><p>aleph-scholar: authorization received. "
            b"You may close this window.</p></body></html>"
        )
        srv.done.set()

    def log_message(self, format: str, *args: object) -> None:
        """Silence per-request stderr logging."""


def run_authorization_flow(
    *,
    authorization_endpoint: str,
    scopes: list[str],
    client_id: str,
    resource: str,
    server: _LoopbackServer,
    redirect_uri: str,
) -> tuple[str, str]:
    """Drive the browser leg; return (authorization_code, code_verifier)."""
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    state = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # RFC 8707 resource indicator — required by the MCP auth spec; ASes
        # that don't support it MUST ignore unknown parameters, so it is
        # harmless to always include.
        "resource": resource,
    }
    if scopes:
        params["scope"] = " ".join(scopes)
    sep = "&" if "?" in authorization_endpoint else "?"
    auth_url = f"{authorization_endpoint}{sep}{urlencode(params)}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print()
    print("Open this URL in your browser to authorize aleph-scholar:")
    print(f"  {auth_url}")
    print()
    if webbrowser.open(auth_url):
        print("(a browser window should have opened automatically)")
    print(f"Waiting up to {CALLBACK_WAIT_SECONDS}s for the redirect on {redirect_uri} ...")

    got = server.done.wait(timeout=CALLBACK_WAIT_SECONDS)
    server.shutdown()
    thread.join(timeout=5)

    if not got:
        fatal("timed out waiting for the OAuth redirect — authorization not completed")
    if server.auth_error:
        fatal(f"authorization server returned an error: {server.auth_error}")
    if server.auth_state != state:
        fatal("state mismatch on the OAuth callback — possible CSRF; aborting")
    if not server.auth_code:
        fatal("OAuth callback did not include an authorization code")
    return server.auth_code, verifier


def exchange_code(
    client: httpx.Client,
    *,
    token_endpoint: str,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
    resource: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "resource": resource,  # RFC 8707 (see note above)
    }
    try:
        resp = client.post(
            token_endpoint,
            data=data,
            headers={"Accept": "application/json"},
        )
    except httpx.HTTPError as exc:
        fatal(f"token exchange failed: {exc}")
    if resp.status_code != 200:
        fatal(f"token endpoint rejected the exchange ({resp.status_code}): {resp.text[:500]}")
    body: Any = resp.json()
    if not isinstance(body, dict):
        fatal("token endpoint returned a non-object response")
    data = cast("dict[str, Any]", body)
    if not data.get("access_token"):
        fatal("token response did not include an access_token")
    return data


# ---------------------------------------------------------------------------
# Steps e/f — store the credential, smoke-check the scholar route
# ---------------------------------------------------------------------------


def put_credential(client: httpx.Client, *, api: str, project: str, blob: dict[str, Any]) -> None:
    url = f"{api}/v1/projects/{project}/connector-credentials/consensus"
    try:
        resp = client.put(
            url,
            json={"plaintext": json.dumps(blob)},
            headers={"Authorization": "Bearer local-dev"},
        )
    except httpx.HTTPError as exc:
        fatal(f"PUT {url} failed: {exc}")
    if resp.status_code == 404:
        fatal(
            f"PUT {url} -> 404. Either the project id is wrong or the 'consensus' "
            "connector row is missing — run `alembic upgrade head` "
            "(migration wp2_scholar_connectors) and retry."
        )
    if resp.status_code != 204:
        fatal(f"PUT {url} -> {resp.status_code}: {resp.text[:500]}")


def enable_binding(client: httpx.Client, *, api: str, project: str) -> None:
    """Enable the project's `consensus` binding.

    Auth-required connectors are seeded DISABLED at project creation
    (`enabled = not requires_auth`); having just authorized the credential,
    enabling is the user's evident intent — without it every search 403s
    with `connector_disabled`.
    """
    headers = {"Authorization": "Bearer local-dev"}
    try:
        connectors = client.get(f"{api}/v1/connectors", headers=headers)
        connectors.raise_for_status()
        connector_id = next(
            (c["id"] for c in connectors.json() if c.get("kind") == "consensus"), None
        )
        if connector_id is None:
            fatal("no 'consensus' connector row — run `alembic upgrade head` and retry.")
        resp = client.post(
            f"{api}/v1/projects/{project}/connectors/bindings",
            json={"connector_id": connector_id, "enabled": True},
            headers=headers,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        fatal(f"enabling the consensus binding failed: {exc}")
    print("    consensus binding enabled for the project")


def smoke_check(client: httpx.Client, *, api: str, project: str) -> None:
    url = f"{api}/v1/projects/{project}/scholar/consensus-search"
    try:
        resp = client.post(
            url,
            json={"query": SMOKE_QUERY},
            headers={"Authorization": "Bearer local-dev"},
        )
    except httpx.HTTPError as exc:
        print(f"    smoke check could not reach the API ({exc}) — credential is stored fine.")
        return
    if resp.status_code == 404:
        print(
            "    scholar routes not deployed yet (404) — credential stored fine; "
            "re-run the search once the WP-2 scholar router ships."
        )
        return
    hits = 0
    try:
        body: Any = resp.json()
        if isinstance(body, dict):
            found: Any = cast("dict[str, Any]", body).get("hits")
            if isinstance(found, list):
                hits = len(cast("list[Any]", found))
    except ValueError:
        pass
    print(f"    consensus-search -> HTTP {resp.status_code}, {hits} hit(s)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="connect-consensus.py",
        description=(
            "Connect a project to Consensus over MCP: OAuth discovery, dynamic "
            "client registration, PKCE browser flow, and credential storage in aleph."
        ),
    )
    parser.add_argument("--project", required=True, help="aleph project UUID")
    parser.add_argument("--api", default="http://localhost:8000", help="aleph API base URL")
    parser.add_argument(
        "--mcp-url",
        default="https://mcp.consensus.app/mcp",
        help="Consensus MCP endpoint",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--client-id",
        default=None,
        help=(
            "pre-registered OAuth client_id (required only when the authorization "
            "server does not support RFC 7591 dynamic registration)"
        ),
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="stop after OAuth discovery (steps a+b); print endpoints and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    api: str = args.api.rstrip("/")
    mcp_url: str = args.mcp_url

    with httpx.Client(timeout=args.timeout, follow_redirects=True) as client:
        step(f"discovering protected-resource metadata for {mcp_url}")
        prm = discover_protected_resource(client, mcp_url)
        auth_servers = _str_list(prm.get("authorization_servers"))
        if not auth_servers:
            fatal("protected-resource metadata lists no authorization_servers")
        issuer = auth_servers[0]
        print(f"    authorization server: {issuer}")

        step(f"discovering authorization-server metadata for {issuer}")
        as_meta = discover_as_metadata(client, issuer)
        authorization_endpoint = str(as_meta["authorization_endpoint"])
        token_endpoint = str(as_meta["token_endpoint"])
        registration_endpoint = as_meta.get("registration_endpoint")

        # Scopes: what the resource advertises, plus offline_access when the AS
        # supports it (needed for a refresh_token on many ASes).
        scopes = _str_list(prm.get("scopes_supported"))
        as_scopes = _str_list(as_meta.get("scopes_supported"))
        if "offline_access" in as_scopes and "offline_access" not in scopes:
            scopes.append("offline_access")

        if args.discover_only:
            print()
            print("Discovery complete (--discover-only):")
            print(f"  resource (MCP URL):      {mcp_url}")
            print(f"  authorization server:    {issuer}")
            print(f"  authorization_endpoint:  {authorization_endpoint}")
            print(f"  token_endpoint:          {token_endpoint}")
            print(f"  registration_endpoint:   {registration_endpoint or '(none advertised)'}")
            print(f"  scopes to request:       {' '.join(scopes) or '(none advertised)'}")
            return 0

        # Loopback server first: registration needs the concrete redirect URI.
        server = _LoopbackServer()
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"

        if args.client_id:
            client_id = str(args.client_id)
            step(f"using pre-registered client_id {client_id}")
        elif registration_endpoint:
            step(f"registering OAuth client at {registration_endpoint}")
            client_id = register_client(client, str(registration_endpoint), redirect_uri)
            print(f"    client_id: {client_id}")
        else:
            server.server_close()
            fatal(
                "the authorization server does not advertise a registration_endpoint "
                "(RFC 7591). Register a public client manually (redirect URI "
                "http://127.0.0.1:<port>/callback, PKCE, no client secret) and re-run "
                "with --client-id <id>."
            )

        step("starting PKCE authorization-code flow (browser required)")
        code, verifier = run_authorization_flow(
            authorization_endpoint=authorization_endpoint,
            scopes=scopes,
            client_id=client_id,
            resource=mcp_url,
            server=server,
            redirect_uri=redirect_uri,
        )

        step("exchanging authorization code for tokens")
        token = exchange_code(
            client,
            token_endpoint=token_endpoint,
            client_id=client_id,
            code=code,
            code_verifier=verifier,
            redirect_uri=redirect_uri,
            resource=mcp_url,
        )
        access_token = str(token["access_token"])
        refresh_token = token.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            fatal(
                "the token response did not include a refresh_token, so the stored "
                "credential could not be refreshed unattended. The authorization "
                "server must grant offline access — some ASes require the "
                "'offline_access' scope; if it appeared in the discovery output, "
                "re-run and approve all requested scopes, otherwise consult the "
                "Consensus MCP docs for how to obtain a refresh token."
            )
        expires_in_raw = token.get("expires_in")
        if isinstance(expires_in_raw, int | float):
            expires_in = int(expires_in_raw)
        else:
            expires_in = 3600
            print("    token response had no expires_in; assuming 3600s")
        expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
        print(f"    access_token:  {mask(access_token)} (expires {expires_at})")
        print(f"    refresh_token: {mask(refresh_token)}")

        blob: dict[str, Any] = {
            "client_id": client_id,
            "token_endpoint": token_endpoint,
            "refresh_token": refresh_token,
            "access_token": access_token,
            "access_token_expires_at": expires_at,
            "status": "active",
        }

        step(f"storing credential via {api}/v1/projects/{args.project}/connector-credentials")
        put_credential(client, api=api, project=str(args.project), blob=blob)
        print("    stored (HTTP 204)")

        step("enabling the project's consensus connector binding")
        enable_binding(client, api=api, project=str(args.project))

        step("smoke-checking POST .../scholar/consensus-search")
        smoke_check(client, api=api, project=str(args.project))

    print()
    print("Done. The Consensus connector credential is stored for this project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

/**
 * Identity for a single-user deployment.
 *
 * Aleph runs one user. The OIDC code-flow that used to live here — issuer,
 * client id, audience, `oidc-client-ts`, a session-storage token store — was
 * removed (`docs/decisions.md` D6): it was half-built, never deployed, and its
 * two known holes were shaping work that had no user waiting on it.
 *
 * What remains is the sentinel bearer the API recognises as the local
 * principal. It is still sent on every request rather than omitted, so the
 * middleware and the log lines look the same as they would with a real token —
 * which is what makes the auth path exercised rather than bypassed.
 *
 * The exported shape is unchanged, so callers did not move. `login`,
 * `logout` and `handleCallback` are retained as no-ops: `App.tsx` still calls
 * them on the mount path, and deleting them would push this decision into the
 * router for no gain.
 */

/** Sentinel bearer. The API ignores its contents and synthesizes the dev principal. */
const LOCAL_BEARER = "local-dev";

const LOCAL_USER = {
  profile: {
    sub: "local-dev",
    email: "dev@aleph.local",
    name: "Local Dev",
  },
  access_token: LOCAL_BEARER,
  expired: false,
} as const;

/** Always true. Kept so `App.tsx` need not branch on a mode that no longer varies. */
export function isLocalAuth(): boolean {
  return true;
}

export async function login(): Promise<void> {
  // No-op: there is nothing to log in to.
}

export async function handleCallback(): Promise<void> {
  // No-op: there is no redirect to handle.
}

export async function logout(): Promise<void> {
  // No-op: there is no session to end.
}

export async function getAccessToken(): Promise<string | null> {
  return LOCAL_BEARER;
}

export async function getCurrentUser() {
  return LOCAL_USER;
}

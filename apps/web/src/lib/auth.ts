import { UserManager, WebStorageStateStore } from "oidc-client-ts";

// `local` (default) bypasses OIDC entirely and returns a sentinel
// bearer that the API recognizes as the local-dev principal.
// `oidc` runs the real OIDC code-flow against the configured issuer.
const authMode = (import.meta.env.VITE_AUTH_MODE as string | undefined) ?? "local";

const issuer = (import.meta.env.VITE_AUTH_ISSUER as string | undefined) ?? "";
const clientId = (import.meta.env.VITE_AUTH_CLIENT_ID as string | undefined) ?? "aleph-web";
const audience = (import.meta.env.VITE_AUTH_AUDIENCE as string | undefined) ?? "aleph";

// Sentinel bearer the API ignores in local mode but still echoes through
// the request path so middleware/log lines look the same as production.
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

// Session-storage only; never localStorage.
const store = typeof window !== "undefined"
  ? new WebStorageStateStore({ store: window.sessionStorage })
  : undefined;

let manager: UserManager | null = null;

function getManager(): UserManager {
  if (manager) return manager;
  if (!issuer) {
    throw new Error("VITE_AUTH_ISSUER not configured (required for VITE_AUTH_MODE=oidc)");
  }
  manager = new UserManager({
    authority: issuer,
    client_id: clientId,
    redirect_uri: `${window.location.origin}/auth/callback`,
    post_logout_redirect_uri: window.location.origin,
    response_type: "code",
    scope: `openid profile email aleph:${audience}`,
    automaticSilentRenew: true,
    userStore: store,
    monitorSession: false,
  });
  return manager;
}

export function isLocalAuth(): boolean {
  return authMode === "local";
}

export async function login(): Promise<void> {
  if (authMode === "local") return;
  await getManager().signinRedirect();
}

export async function handleCallback(): Promise<void> {
  if (authMode === "local") return;
  await getManager().signinRedirectCallback();
}

export async function logout(): Promise<void> {
  if (authMode === "local") return;
  await getManager().signoutRedirect();
}

export async function getAccessToken(): Promise<string | null> {
  if (authMode === "local") return LOCAL_BEARER;
  try {
    const user = await getManager().getUser();
    if (!user || user.expired) return null;
    return user.access_token;
  } catch {
    return null;
  }
}

export async function getCurrentUser() {
  if (authMode === "local") return LOCAL_USER;
  return getManager().getUser();
}

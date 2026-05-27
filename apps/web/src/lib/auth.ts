import { UserManager, WebStorageStateStore } from "oidc-client-ts";

const issuer = (import.meta.env.VITE_AUTH_ISSUER as string | undefined) ?? "";
const clientId = (import.meta.env.VITE_AUTH_CLIENT_ID as string | undefined) ?? "aleph-web";
const audience = (import.meta.env.VITE_AUTH_AUDIENCE as string | undefined) ?? "aleph";

// Session-storage only; never localStorage.
const store = new WebStorageStateStore({ store: window.sessionStorage });

let manager: UserManager | null = null;

function getManager(): UserManager {
  if (manager) return manager;
  if (!issuer) {
    throw new Error("VITE_AUTH_ISSUER not configured");
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

export async function login(): Promise<void> {
  await getManager().signinRedirect();
}

export async function handleCallback(): Promise<void> {
  await getManager().signinRedirectCallback();
}

export async function logout(): Promise<void> {
  await getManager().signoutRedirect();
}

export async function getAccessToken(): Promise<string | null> {
  try {
    const user = await getManager().getUser();
    if (!user || user.expired) return null;
    return user.access_token;
  } catch {
    return null;
  }
}

export async function getCurrentUser() {
  return getManager().getUser();
}

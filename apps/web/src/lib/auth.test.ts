/**
 * Identity for a single-user deployment.
 *
 * The OIDC code-flow that used to branch here was removed (docs/decisions.md
 * D6). What is pinned is the property that survived the removal: a sentinel
 * bearer is still SENT on every request rather than omitted, so the auth
 * middleware and the log lines look the same as they would with a real token.
 * Returning `null` instead would make the client stop sending the header, and
 * the API path that checks it would stop being exercised — a bypass that reads
 * as "auth works" because nothing 401s.
 */
import { describe, expect, it } from "vitest";

import { getAccessToken, getCurrentUser, handleCallback, isLocalAuth, login, logout } from "@/lib/auth";

describe("auth", () => {
  it("hands out a token rather than nothing, so the auth path stays exercised", async () => {
    await expect(getAccessToken()).resolves.toBe("local-dev");
  });

  it("reports local mode", () => {
    expect(isLocalAuth()).toBe(true);
  });

  it("describes the local principal", async () => {
    const user = await getCurrentUser();
    expect(user.access_token).toBe("local-dev");
    expect(user.expired).toBe(false);
    expect(user.profile.sub).toBe("local-dev");
  });

  it("keeps login/logout/handleCallback callable, because App.tsx still calls them", async () => {
    await expect(login()).resolves.toBeUndefined();
    await expect(logout()).resolves.toBeUndefined();
    await expect(handleCallback()).resolves.toBeUndefined();
  });
});

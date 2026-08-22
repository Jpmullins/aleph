/**
 * The one-line side-effect module that keeps the assistant able to connect.
 *
 * CopilotKit's v2 transport hands an UNBOUND reference to the native `fetch`
 * into rxjs, which then calls it with a receiver that is not `window`. Chrome
 * rejects that with "Illegal invocation", which surfaces as
 * `agent_connect_failed` — the chat is simply dead, with an error that names
 * neither this module nor the library.
 *
 * The test reproduces the receiver check rather than trusting jsdom, whose
 * `fetch` is permissive: an assertion that only holds because the environment
 * is lenient would keep passing after the fix was deleted.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

function receiverCheckingFetch() {
  return function (this: unknown) {
    if (this !== window) throw new TypeError("Failed to execute 'fetch' on 'Window'");
    return "called";
  };
}

describe("fetch-bind", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("replaces the global with a reference safe to call with any receiver", async () => {
    const native = receiverCheckingFetch();
    vi.stubGlobal("fetch", native);
    window.fetch = native as unknown as typeof fetch;

    await import("@/lib/fetch-bind");

    const detached = window.fetch as unknown as () => string;
    expect(detached).not.toBe(native);
    expect(detached()).toBe("called");
  });

  it("leaves the unbound reference failing, which is what the module exists for", () => {
    const native = receiverCheckingFetch();
    expect(() => (native as unknown as () => string)()).toThrow(/Illegal|Failed to execute/);
  });
});

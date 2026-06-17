/**
 * Bind the global `fetch` to `window`.
 *
 * CopilotKit's v2 transport (`@copilotkit/react-core/v2`, via its bundled
 * AG-UI client) issues requests through RxJS `fromFetch(url, { fetch:
 * environment.fetch })`. That passes an *unbound* reference to the native
 * `fetch` into rxjs, which invokes it with a receiver that is not `window`.
 * Chrome rejects that with `TypeError: Failed to execute 'fetch' on 'Window':
 * Illegal invocation`, which surfaces as `agent_connect_failed` /
 * `agent_run_failed` and breaks the assistant entirely.
 *
 * Re-binding the global once, before any CopilotKit code captures it, makes the
 * reference safe to call with any receiver. This is a known, library-agnostic
 * workaround for the rxjs-`fromFetch` + custom-`fetch` "Illegal invocation"
 * class of bug and is a no-op once the upstream transport binds fetch itself.
 *
 * Imported for its side effect as the very first import in `main.tsx`.
 */
if (typeof window !== "undefined" && typeof window.fetch === "function") {
  const native = window.fetch;
  window.fetch = native.bind(window);
}

export {};

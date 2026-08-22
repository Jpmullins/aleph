/**
 * Boot the Node bridge and check what it will actually do. WS-D3.
 *
 * `apps/copilot-runtime` sat on `cors: true` and constructed
 * `new HttpAgent({ url: AGENT_URL })` with no headers. Two separate problems
 * with one appearance:
 *
 *   1. Any origin could drive the agent from a browser. A page the user had
 *      open could POST to :4000 and read their project, spend their tokens,
 *      write to their wiki — and the request is indistinguishable from the real
 *      UI's, so nothing would have recorded anything unusual.
 *   2. The bridge called the API anonymously, so the API saw the BRIDGE and
 *      never the person.
 *
 * Forwarding is checked in BOTH directions. A bridge that substitutes a
 * credential of its own when the caller sent none passes a forwarding check
 * that only ever sends a credential, and every anonymous request then reaches
 * the API looking authenticated — attributed, in the action ledger, to whoever
 * that credential belongs to.
 *
 * This is a probe rather than a unit test because there is no JS test framework
 * in this repo yet (WS-UI-2), and because a source grep for `cors:` would pass
 * against a config that does not do what it says. Here the server is started
 * for real, a fake API stands in for `aleph-api`, and the assertions are about
 * bytes on the wire.
 *
 * WHAT THIS DOES NOT PROVE, said plainly: CORS is enforced by BROWSERS. A
 * missing `Access-Control-Allow-Origin` stops a malicious page from using
 * someone's session; it does nothing about `curl`, and it never could. What
 * narrows raw reachability is the publish address, checked separately in
 * `scripts/check-runtime-bridge.sh`.
 */
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const RUNTIME_DIR = path.join(ROOT, "apps/copilot-runtime");
const ALLOWED = "http://localhost:5173";
const EVIL = "https://evil.example";

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "ok  " : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures += 1;
};

/** A stand-in for aleph-api that records what the bridge sent it. */
function fakeApi() {
  const seen = [];
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      seen.push({ authorization: req.headers.authorization });
      res.writeHead(200, { "content-type": "text/event-stream" });
      res.end("data: {}\n\n");
    });
  });
  return { server, seen };
}

const listen = (server, port) =>
  new Promise((resolve) => server.listen(port, "127.0.0.1", resolve));

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const { server: api, seen } = fakeApi();
  await listen(api, 0);
  const apiPort = api.address().port;

  // A port the OS says is free, not a constant.
  //
  // A fixed port let a leftover child from an earlier run hold 4319 — the new
  // child died with EADDRINUSE, the health check then succeeded against the
  // STALE server (pointed at a fake API that had already closed), and the probe
  // reported "the API was never called" for a bridge that was working. A
  // hard-coded port turned a clean-up bug into a false failure, and would just
  // as easily have turned into a false pass.
  const scratch = createServer();
  await listen(scratch, 0);
  const runtimePort = scratch.address().port;
  await new Promise((r) => scratch.close(r));
  const child = spawn("npx", ["tsx", "src/server.ts"], {
    cwd: RUNTIME_DIR,
    env: {
      ...process.env,
      PORT: String(runtimePort),
      ALEPH_AGENT_URL: `http://127.0.0.1:${apiPort}/copilotkit/agent/assistant`,
      ALEPH_CORS_ORIGINS: ALLOWED,
      ALEPH_RUNTIME_BIND: "127.0.0.1",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let log = "";
  child.stdout.on("data", (d) => (log += d));
  child.stderr.on("data", (d) => (log += d));

  const base = `http://127.0.0.1:${runtimePort}`;
  let up = false;
  for (let i = 0; i < 60; i += 1) {
    try {
      const r = await fetch(`${base}/health`);
      if (r.ok) {
        up = true;
        break;
      }
    } catch {
      /* not listening yet */
    }
    await wait(500);
  }

  try {
    if (!up) {
      // Exit 1, not 0. `run_shell` in `scripts/acceptance.sh` has no way to
      // record a SKIP for a row it already decided to run: it reads the exit
      // status and prints the last line, so `return 0` here made row F5 report
      // PASS with "SKIP: the runtime did not start" as its detail, and a bridge
      // that cannot boot at all was the strongest possible version of the
      // defect this probe exists to catch. The row is already guarded on `node`
      // being present (acceptance.sh skips F5 otherwise), so reaching here
      // means node is installed and the bridge still would not come up.
      console.log(log.slice(-600));
      console.log(
        "FAIL: the runtime did not start after 30s — install its deps " +
          "(`pnpm -C apps/copilot-runtime install`) or check the log above",
      );
      return 1;
    }
    console.log("runtime bridge probe");

    // 1. An unlisted origin gets no grant. This is THE criterion.
    const evil = await fetch(`${base}/api/copilotkit`, {
      method: "OPTIONS",
      headers: {
        Origin: EVIL,
        "Access-Control-Request-Method": "POST",
      },
    });
    const evilGrant = evil.headers.get("access-control-allow-origin");
    check(
      "an unlisted origin receives no Access-Control-Allow-Origin",
      evilGrant !== EVIL && evilGrant !== "*",
      `got ${evilGrant ?? "(none)"}`,
    );

    // 2. ...and the real UI still works. Half a fix that blocks everyone is not
    //    a fix, and this is the assertion that stops "deny everything" passing.
    const good = await fetch(`${base}/api/copilotkit`, {
      method: "OPTIONS",
      headers: {
        Origin: ALLOWED,
        "Access-Control-Request-Method": "POST",
      },
    });
    check(
      "the configured origin is still allowed",
      good.headers.get("access-control-allow-origin") === ALLOWED,
      `got ${good.headers.get("access-control-allow-origin") ?? "(none)"}`,
    );

    // 3. The caller's credential reaches the API.
    // `agent/run` is the route that forwards to the agent — POSTing the base
    // path returns 404 and would make this check vacuously "the API was never
    // called". Enumerated from the installed handler, not guessed.
    // `/agent/:agentId/run`, with the agent id segment. Read out of the
    // installed router (`matchRoute` in fetch-router), not guessed: this
    // handler is in multi-route mode, and both `/api/copilotkit` and
    // `/api/copilotkit/agent/run` return 404 — which would make this check
    // pass vacuously as "the API was never called".
    const drive = async (authorization) => {
      const before = seen.length;
      const headers = { "content-type": "application/json", Origin: ALLOWED };
      if (authorization !== undefined) headers.Authorization = authorization;
      await fetch(`${base}/api/copilotkit/agent/assistant/run`, {
        method: "POST",
        headers,
        // The runtime runs in "single-route" mode: one POST to the base path
        // carrying a `{method, params}` envelope. Read out of the installed
        // handler (`resolveSingleRoute`), not guessed — the RunAgentInput shape
        // the agent itself takes returns 404 here, which would have made this
        // check pass vacuously as "the API was never called".
        body: JSON.stringify({
          threadId: "proj:00000000-0000-0000-0000-000000000000:probe",
          runId: "probe",
          messages: [{ id: "m1", role: "user", content: "hi" }],
          tools: [],
          context: [],
          state: {},
          forwardedProps: {},
        }),
      })
        .then(async (r) => {
          if (!r.ok) {
            console.log(`     (runtime returned ${r.status}: ${(await r.text()).slice(0, 200)})`);
            return;
          }
          // The response is an SSE stream and the agent is called as it is
          // CONSUMED. Leaving it unread makes this check report "the API was
          // never called" for a bridge that works perfectly.
          // Drained, not inspected. The agent is called as the SSE stream is
          // CONSUMED, so leaving it unread reports "the API was never called"
          // for a bridge that works. What comes back is the fake API's reply
          // failing AG-UI validation, which is expected and not the subject.
          await r.text();
        })
        .catch((e) => console.log(`     (post failed: ${e})`));

      for (let i = 0; i < 20 && seen.length === before; i += 1) await wait(250);
      return seen.length > before ? seen[seen.length - 1] : null;
    };

    const withCredential = await drive("Bearer probe-token");
    check(
      "the caller's Authorization header reaches the API",
      withCredential !== null && withCredential.authorization === "Bearer probe-token",
      withCredential === null ? "the API was never called" : `got ${withCredential.authorization}`,
    );

    // 4. ...and the OTHER direction, which is the half nothing asserted.
    //
    // Forwarding is only half the property. A bridge that falls back to a
    // credential of its OWN when the caller sent none is worse than one that
    // forwards nothing: every anonymous request would arrive at the API looking
    // like an authenticated one, the API would authorise it, and the action
    // ledger would attribute it to whoever the bridge's credential belongs to.
    // Check 3 passes either way, because check 3 only ever sends a credential.
    //
    // Structurally the bridge cannot do this today — `server.ts` builds
    // `new HttpAgent({ url: AGENT_URL })` with no headers and no `fetch`
    // override, and the compose env carries no token for it to reach for. That
    // is an argument, not a check: any of those three could change in a commit
    // whose diff looks like a convenience.
    const anonymous = await drive(undefined);
    check(
      "an unauthenticated request is not given a credential of the bridge's own",
      anonymous !== null && anonymous.authorization === undefined,
      anonymous === null
        ? "the API was never called"
        : `got ${anonymous.authorization ?? "(none)"}`,
    );
    if (failures > 0 && log.trim()) {
      console.log("\n  --- runtime log (tail) ---");
      console.log(log.split("\n").slice(-12).map((l) => `  ${l}`).join("\n"));
    }
  } finally {
    child.kill("SIGKILL");
    api.close();
    // The child spawns `tsx`, which spawns node; killing the wrapper can leave
    // the listener holding the port. Give it a moment to actually go.
    await wait(300);
  }

  console.log(failures === 0 ? "\nOK" : `\nFAIL: ${failures} check(s)`);
  return failures === 0 ? 0 : 1;
}

process.exit(await main());

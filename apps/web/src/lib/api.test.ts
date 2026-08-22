/**
 * The API client — the one place every request's credential is attached.
 *
 * A missing Authorization header here is not a visible failure: the API answers
 * 401, react-query reports an error, and the UI shows an empty list. That looks
 * like a project with no data.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, apiUrl } from "@/lib/api";

interface Call {
  url: string;
  init: RequestInit;
}

let calls: Call[] = [];
// A factory, not a value: a `Response` body can be read exactly once, so a
// shared instance makes the SECOND request in a test fail with "Body is
// unusable" — a fixture defect that reads as a client defect.
let reply: () => Response;

function json(body: unknown, status = 200): () => Response {
  return () =>
    new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json" },
    });
}

beforeEach(() => {
  calls = [];
  reply = json({ ok: true });
  vi.stubGlobal("fetch", (url: string, init: RequestInit) => {
    calls.push({ url, init });
    return Promise.resolve(reply());
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function headersOf(index = 0): Record<string, string> {
  return calls[index].init.headers as Record<string, string>;
}

describe("api", () => {
  it("attaches the bearer token to every request", async () => {
    await api.get("/v1/projects");
    expect(headersOf().Authorization).toBe("Bearer local-dev");
  });

  it("never sends cookies, so a stolen session cannot ride along", async () => {
    await api.get("/v1/projects");
    expect(calls[0].init.credentials).toBe("omit");
  });

  it("sets Content-Type only when there is a body to type", async () => {
    await api.get("/v1/projects");
    expect(headersOf(0)["Content-Type"]).toBeUndefined();
    await api.post("/v1/projects", { title: "x" });
    expect(headersOf(1)["Content-Type"]).toBe("application/json");
    expect(calls[1].init.body).toBe(JSON.stringify({ title: "x" }));
  });

  it("sends the method the caller asked for", async () => {
    await api.patch("/v1/projects/1", { status: "deleted" });
    await api.del("/v1/projects/1");
    expect(calls.map((c) => c.init.method)).toEqual(["PATCH", "DELETE"]);
  });

  it("prefixes the configured base URL", async () => {
    await api.get("/v1/projects");
    expect(calls[0].url).toBe("http://localhost:8000/v1/projects");
    expect(apiUrl("/v1/assets/1")).toBe("http://localhost:8000/v1/assets/1");
  });

  it("raises ApiError carrying the status and the parsed body", async () => {
    reply = json({ detail: "nope" }, 403);
    await expect(api.get("/v1/projects")).rejects.toBeInstanceOf(ApiError);

    let err: ApiError | null = null;
    try {
      await api.get("/v1/projects");
    } catch (e) {
      err = e as ApiError;
    }
    expect(err?.status).toBe(403);
    expect(err?.body).toEqual({ detail: "nope" });
  });

  it("does not try to parse a 204 as JSON", async () => {
    // `new Response(null, {status: 204})` has no body; calling .json() on it
    // rejects. A client that always parses turns every successful delete into
    // an error the caller reports as a failed delete.
    reply = () => new Response(null, { status: 204 });
    await expect(api.del("/v1/projects/1")).resolves.toBeUndefined();
  });
});

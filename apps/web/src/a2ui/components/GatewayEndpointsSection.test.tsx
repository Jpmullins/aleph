/**
 * The screen five gateway routes never had, and the three rules it must keep.
 *
 * WS-MEP-5. `GET/PUT/DELETE /v1/projects/{id}/gateway-endpoints` plus the
 * per-row `/test` probe shipped with `grep -rn 'gateway-endpoints'
 * apps/web/src` returning 0. These pin the properties that make the screen
 * safe to have rather than merely present:
 *
 *   1. the key input is a password field and is never given a value from a
 *      response;
 *   2. a failed probe shows the gateway's own sentence, not "failed";
 *   3. a non-owner is told the list is withheld, not shown an empty one.
 *
 * The mutation for (1) is the one with consequences: make the row carry a key
 * and render it, and `the key input is never populated from a response` goes
 * red while everything else stays green.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, waitFor, type RenderResult } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const post = vi.fn();
const put = vi.fn();
const del = vi.fn();
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    api: {
      post: (path: string, body: unknown) => post(path, body),
      put: (path: string, body: unknown) => put(path, body),
      del: (path: string) => del(path),
    },
  };
});

import {
  GatewayEndpoints,
  type GatewayEndpointRow,
  type GatewayEndpointsSectionData,
} from "@/a2ui/components/GatewayEndpointsSection";

const ROW: GatewayEndpointRow = {
  id: "01a0-endpoint",
  name: "litellm-prod",
  base_url: "https://gateway.example.test",
  is_default: true,
  has_api_key: true,
  key_version: "v2",
  last_probe_at: "2026-08-22T12:00:00+00:00",
  last_probe_ok: false,
  last_probe_error: "AuthenticationError: invalid api key",
  last_probe_model_count: 0,
};

function mount(section: GatewayEndpointsSectionData): RenderResult {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <GatewayEndpoints section={section} projectId="proj-1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  post.mockReset();
  put.mockReset();
  del.mockReset();
});

describe("GatewayEndpoints", () => {
  it("offers a base URL field and a password-typed key field", () => {
    const view = mount({ can_edit: true, endpoints: [] });
    fireEvent.click(view.getByTestId("gateway-endpoint-add"));
    const url = view.getByTestId("gateway-endpoint-base-url-input");
    const key = view.getByTestId("gateway-endpoint-api-key-input");
    expect(url.getAttribute("type")).not.toBe("password");
    expect(key.getAttribute("type")).toBe("password");
    // A browser that offers to autofill a saved password into a field that
    // rotates a server credential is a way to overwrite one by accident.
    expect(key.getAttribute("autocomplete")).toBe("new-password");
  });

  it("never populates the key input from anything the server sent", () => {
    const view = mount({ can_edit: true, endpoints: [ROW] });
    // The row says a key exists. That must not put one on screen.
    expect(view.getByTestId(`gateway-endpoint-${ROW.name}`).textContent).toContain("key set");
    fireEvent.click(view.getByTestId(`gateway-endpoint-replace-key-${ROW.name}`));
    const input = view.getByTestId(`gateway-endpoint-key-${ROW.name}`) as HTMLInputElement;
    expect(input.type).toBe("password");
    expect(input.value).toBe("");
  });

  it("shows the gateway's own words when the last probe failed", () => {
    const view = mount({ can_edit: true, endpoints: [ROW] });
    const shown = view.getByTestId(`gateway-endpoint-error-${ROW.name}`).textContent;
    // Verbatim. "Could not connect" sends an operator to look at the network
    // when the answer was an auth failure.
    expect(shown).toBe("AuthenticationError: invalid api key");
  });

  it("replaces the stored error with what Test connection just heard", async () => {
    post.mockResolvedValue({
      ok: false,
      model_count: 0,
      model_info_allowed: null,
      models: [],
      error: "ConnectError: [Errno 61] Connection refused",
    });
    const view = mount({ can_edit: true, endpoints: [ROW] });
    fireEvent.click(view.getByTestId(`gateway-endpoint-test-${ROW.name}`));
    await waitFor(() =>
      expect(view.getByTestId(`gateway-endpoint-error-${ROW.name}`).textContent).toBe(
        "ConnectError: [Errno 61] Connection refused",
      ),
    );
    expect(post).toHaveBeenCalledWith(
      "/v1/projects/proj-1/gateway-endpoints/01a0-endpoint/test",
      {},
    );
  });

  it("reports a reachable endpoint with the model count it answered", async () => {
    post.mockResolvedValue({
      ok: true,
      model_count: 7,
      model_info_allowed: false,
      models: [],
      error: null,
    });
    const view = mount({ can_edit: true, endpoints: [ROW] });
    fireEvent.click(view.getByTestId(`gateway-endpoint-test-${ROW.name}`));
    await waitFor(() => view.getByTestId(`gateway-endpoint-ok-${ROW.name}`));
    const text = view.getByTestId(`gateway-endpoint-ok-${ROW.name}`).textContent ?? "";
    expect(text).toContain("7 model(s)");
    // `/model/info` restricted is the NORMAL answer for a scoped virtual key,
    // and saying so is the difference between "ids only" and "broken".
    expect(text).toContain("/model/info is restricted");
  });

  it("keeps the stored key when only the default is being changed", async () => {
    put.mockResolvedValue({});
    const other: GatewayEndpointRow = { ...ROW, id: "other", name: "staging", is_default: false };
    const view = mount({ can_edit: true, endpoints: [other] });
    fireEvent.click(view.getByTestId("gateway-endpoint-default-staging"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    const body = put.mock.calls[0][1] as Record<string, unknown>;
    // OMITTED, not null and not "". The API reads all three differently:
    // absent keeps, "" clears, a value rotates. Sending "" here would wipe the
    // credential every time somebody promoted an endpoint.
    expect("api_key" in body).toBe(false);
    expect(body.is_default).toBe(true);
  });

  it("makes the first endpoint the default, and later ones not", async () => {
    put.mockResolvedValue({});
    const view = mount({ can_edit: true, endpoints: [] });
    fireEvent.click(view.getByTestId("gateway-endpoint-add"));
    fireEvent.change(view.getByTestId("gateway-endpoint-name-input"), {
      target: { value: "first" },
    });
    fireEvent.change(view.getByTestId("gateway-endpoint-base-url-input"), {
      target: { value: "https://one.example.test" },
    });
    fireEvent.click(view.getByTestId("gateway-endpoint-save"));
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1));
    expect((put.mock.calls[0][1] as Record<string, unknown>).is_default).toBe(true);
  });

  it("tells a non-owner the list is withheld rather than showing an empty one", () => {
    const view = mount({ can_edit: false, endpoints: [] });
    expect(view.queryByTestId("gateway-endpoints")).toBeNull();
    expect(view.getByTestId("gateway-endpoints-withheld").textContent).toContain(
      "Owner access required",
    );
  });

  it("names the deployment default when no row exists yet", () => {
    const view = mount({
      can_edit: true,
      endpoints: [],
      fallback_base_url: "http://litellm:4000",
    });
    // "No endpoints" must not read as "no gateway" — the process still has one.
    expect(view.getByTestId("gateway-endpoints-empty").textContent).toContain(
      "http://litellm:4000",
    );
  });
});

/**
 * Where this project's model calls go — the screen five routes never had.
 *
 * WS-MEP-5. `GET/PUT/DELETE /v1/projects/{id}/gateway-endpoints`, the per-row
 * `/test` probe and the resolver behind them all shipped with
 * `grep -rn 'gateway-endpoints' apps/web/src` returning **0**: a table, a
 * cipher, a probe and a resolver reachable only by curl. Aleph serves no models
 * and ships no gateway, so "point it somewhere" is the first thing an operator
 * has to do, and until now the only way to do it was to redeploy with a
 * different `LITELLM_BASE_URL`.
 *
 * Three properties this file is responsible for:
 *
 * 1. **The key is write-only.** The password input is never populated from a
 *    response — `GatewayEndpointOut` carries no key at all, only `has_api_key`
 *    and `key_version`, and there is no code path here that could put one on
 *    screen. Editing a URL does not require retyping the key: an omitted
 *    `api_key` keeps whatever the row has, `""` clears it, a value rotates it.
 *    Those three intentions are the API's, and the form expresses all three.
 * 2. **The key goes over REST, never through a card action.** A settings value
 *    dispatched as an A2UI action is persisted to `card_actions` and to the
 *    append-only ledger, which is why `settings_card` refuses a field that
 *    declares itself a secret. `PUT` is the only writer.
 * 3. **A failed probe shows the gateway's own words.** `last_probe_error` and
 *    the probe response's `error` are rendered verbatim. "Connection failed"
 *    sends an operator to look at the network when the endpoint said
 *    `invalid api key`, and the route deliberately answers 200 with the
 *    upstream text for exactly that reason.
 *
 * Reads are bound: every row below arrives in the settings surface's
 * `sections` prop from the multiplexed stream. This component fetches nothing.
 * Writes are calls, and the stream pushes the new state back — `pending` is a
 * disabled state, not a shadow copy.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api, errMsg } from "@/lib/api";

export interface GatewayEndpointRow {
  id: string;
  name: string;
  base_url: string;
  is_default: boolean;
  /** Whether a key is stored. Never the key, and never a prefix of it. */
  has_api_key: boolean;
  key_version: string | null;
  last_probe_at: string | null;
  last_probe_ok: boolean | null;
  last_probe_error: string | null;
  last_probe_model_count: number | null;
}

export interface GatewayEndpointsSectionData {
  /** False for a non-owner: the five REST routes are owner-gated, so the list
   *  is withheld rather than empty, and the blurb says so. */
  can_edit?: boolean;
  endpoints?: GatewayEndpointRow[];
  /** `LITELLM_BASE_URL`, used when no row claims default. Shown so "no
   *  endpoints configured" does not read as "no gateway". */
  fallback_base_url?: string | null;
}

interface ProbeResult {
  ok: boolean;
  model_count: number;
  model_info_allowed: boolean | null;
  models: string[];
  error: string | null;
}

function arr<T>(value: T[] | undefined): T[] {
  return Array.isArray(value) ? value : [];
}

export function GatewayEndpoints({
  section,
  projectId,
}: {
  section: GatewayEndpointsSectionData;
  projectId: string;
}) {
  const endpoints = arr(section.endpoints);

  if (section.can_edit !== true) {
    return (
      <p className="text-xs text-ink-muted" data-testid="gateway-endpoints-withheld">
        Owner access required to read or change this project's gateway endpoints.
      </p>
    );
  }

  return (
    <div data-testid="gateway-endpoints">
      {endpoints.length === 0 ? (
        <p className="mb-2 text-xs text-ink-muted" data-testid="gateway-endpoints-empty">
          {section.fallback_base_url
            ? `No endpoint row yet — calls go to the deployment default, ${section.fallback_base_url}.`
            : "No endpoint row yet, and no deployment default is configured."}
        </p>
      ) : (
        <ul className="mb-3 space-y-2">
          {endpoints.map((e) => (
            <EndpointItem key={e.id} endpoint={e} projectId={projectId} />
          ))}
        </ul>
      )}
      <AddEndpoint projectId={projectId} hasAny={endpoints.length > 0} />
    </div>
  );
}

function EndpointItem({
  endpoint,
  projectId,
}: {
  endpoint: GatewayEndpointRow;
  projectId: string;
}) {
  const qc = useQueryClient();
  const [replacing, setReplacing] = useState(false);
  const [keyDraft, setKeyDraft] = useState("");

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ["gateway-endpoints", projectId] });
  };

  const probe = useMutation<ProbeResult>({
    mutationFn: () =>
      api.post<ProbeResult>(
        `/v1/projects/${projectId}/gateway-endpoints/${endpoint.id}/test`,
        {},
      ),
    onSuccess: invalidate,
  });

  // `api_key` is deliberately ABSENT from this body, not null-ed: omitted means
  // "keep the stored key". Sending `api_key: ""` here would silently clear the
  // credential every time somebody pressed Make default.
  const makeDefault = useMutation({
    mutationFn: () =>
      api.put(`/v1/projects/${projectId}/gateway-endpoints`, {
        name: endpoint.name,
        base_url: endpoint.base_url,
        is_default: true,
      }),
    onSuccess: invalidate,
  });

  const rotate = useMutation({
    mutationFn: (plaintext: string) =>
      api.put(`/v1/projects/${projectId}/gateway-endpoints`, {
        name: endpoint.name,
        base_url: endpoint.base_url,
        is_default: endpoint.is_default,
        api_key: plaintext,
      }),
    onSuccess: () => {
      setKeyDraft("");
      setReplacing(false);
      invalidate();
    },
  });

  const remove = useMutation({
    mutationFn: () => api.del(`/v1/projects/${projectId}/gateway-endpoints/${endpoint.id}`),
    onSuccess: invalidate,
  });

  const probed = probe.data;
  // The freshly probed answer wins over the stored one; both are the endpoint's
  // own words either way.
  const failure = probed ? (probed.ok ? null : probed.error) : endpoint.last_probe_error;
  const reachable = probed ? probed.ok : endpoint.last_probe_ok;

  return (
    <li
      className="border border-line px-3 py-2"
      data-testid={`gateway-endpoint-${endpoint.name}`}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="truncate text-sm font-medium text-ink">{endpoint.name}</span>
            {endpoint.is_default && (
              <span className="shrink-0 bg-badge-neutral-bg px-1 py-px text-[10px] uppercase tracking-wider text-badge-neutral-fg">
                default
              </span>
            )}
          </div>
          <div className="truncate font-mono text-[11px] text-ink-muted">{endpoint.base_url}</div>
          <div className="text-[10px] uppercase tracking-wider text-ink-muted">
            {endpoint.has_api_key
              ? `key set${endpoint.key_version ? ` · ${endpoint.key_version}` : ""}`
              : "no key"}
            {endpoint.last_probe_model_count !== null &&
              ` · ${endpoint.last_probe_model_count} model(s) at last probe`}
          </div>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <button
            type="button"
            data-testid={`gateway-endpoint-test-${endpoint.name}`}
            disabled={probe.isPending}
            onClick={() => probe.mutate()}
            className="border border-line-strong px-2 py-1 text-[11px] font-medium text-ink-soft hover:bg-elevated disabled:opacity-50"
          >
            {probe.isPending ? "Testing…" : "Test connection"}
          </button>
          {!endpoint.is_default && (
            <button
              type="button"
              data-testid={`gateway-endpoint-default-${endpoint.name}`}
              disabled={makeDefault.isPending}
              onClick={() => makeDefault.mutate()}
              className="border border-line-strong px-2 py-1 text-[11px] text-ink-soft hover:bg-elevated disabled:opacity-50"
            >
              Make default
            </button>
          )}
          <button
            type="button"
            data-testid={`gateway-endpoint-remove-${endpoint.name}`}
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
            className="border border-line-strong px-2 py-1 text-[11px] text-ink-soft hover:bg-elevated disabled:opacity-50"
          >
            Remove
          </button>
        </div>
      </div>

      {/* The verbatim answer. `failure` is the gateway's sentence, not ours. */}
      {failure ? (
        <p
          className="mt-1.5 break-words text-[11px] text-bad"
          data-testid={`gateway-endpoint-error-${endpoint.name}`}
        >
          {failure}
        </p>
      ) : reachable === true ? (
        <p
          className="mt-1.5 text-[11px] text-good"
          data-testid={`gateway-endpoint-ok-${endpoint.name}`}
        >
          Answered
          {probed
            ? ` with ${probed.model_count} model(s)${
                probed.model_info_allowed === false
                  ? " — /model/info is restricted, so ids only"
                  : ""
              }`
            : endpoint.last_probe_at
              ? ` at ${endpoint.last_probe_at}`
              : ""}
          .
        </p>
      ) : null}
      {probe.isError && <p className="mt-1.5 text-[11px] text-bad">{errMsg(probe.error)}</p>}
      {(makeDefault.isError || remove.isError) && (
        <p className="mt-1.5 text-[11px] text-bad">
          {errMsg(makeDefault.error ?? remove.error)}
        </p>
      )}

      {replacing ? (
        <form
          className="mt-2 flex items-center gap-1.5"
          onSubmit={(ev) => {
            ev.preventDefault();
            rotate.mutate(keyDraft);
          }}
        >
          <input
            type="password"
            autoComplete="new-password"
            aria-label={`New API key for ${endpoint.name}`}
            data-testid={`gateway-endpoint-key-${endpoint.name}`}
            value={keyDraft}
            onChange={(ev) => setKeyDraft(ev.target.value)}
            placeholder="new key (empty clears it)"
            className="min-w-0 flex-1 border border-line-strong bg-surface px-1.5 py-1 text-[11px] text-ink"
          />
          <button
            type="submit"
            disabled={rotate.isPending}
            className="border border-line-strong px-2 py-1 text-[11px] text-ink-soft hover:bg-elevated disabled:opacity-50"
          >
            Save key
          </button>
          <button
            type="button"
            onClick={() => {
              setKeyDraft("");
              setReplacing(false);
            }}
            className="px-1 text-[11px] text-ink-muted hover:text-ink"
          >
            Cancel
          </button>
        </form>
      ) : (
        <button
          type="button"
          data-testid={`gateway-endpoint-replace-key-${endpoint.name}`}
          onClick={() => setReplacing(true)}
          className="mt-1.5 text-[11px] text-ink-muted underline hover:text-ink"
        >
          {endpoint.has_api_key ? "Replace key" : "Add key"}
        </button>
      )}
      {rotate.isError && <p className="mt-1 text-[11px] text-bad">{errMsg(rotate.error)}</p>}
    </li>
  );
}

function AddEndpoint({ projectId, hasAny }: { projectId: string; hasAny: boolean }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");

  const save = useMutation({
    mutationFn: () =>
      api.put(`/v1/projects/${projectId}/gateway-endpoints`, {
        name,
        base_url: baseUrl,
        api_key: apiKey,
        // The first endpoint a project gets is its default; otherwise adding
        // one would change nothing and read as a no-op.
        is_default: !hasAny,
      }),
    onSuccess: () => {
      setName("");
      setBaseUrl("");
      setApiKey("");
      setOpen(false);
      void qc.invalidateQueries({ queryKey: ["gateway-endpoints", projectId] });
    },
  });

  if (!open) {
    return (
      <button
        type="button"
        data-testid="gateway-endpoint-add"
        onClick={() => setOpen(true)}
        className="border border-line-strong px-2 py-1 text-[11px] font-medium text-ink-soft hover:bg-elevated"
      >
        Add endpoint
      </button>
    );
  }

  return (
    <form
      className="space-y-1.5 border border-line px-3 py-2"
      data-testid="gateway-endpoint-form"
      onSubmit={(ev) => {
        ev.preventDefault();
        save.mutate();
      }}
    >
      <input
        aria-label="Endpoint name"
        data-testid="gateway-endpoint-name-input"
        value={name}
        onChange={(ev) => setName(ev.target.value)}
        placeholder="name, e.g. litellm-prod"
        className="w-full border border-line-strong bg-surface px-1.5 py-1 text-[11px] text-ink"
      />
      <input
        aria-label="Endpoint base URL"
        data-testid="gateway-endpoint-base-url-input"
        value={baseUrl}
        onChange={(ev) => setBaseUrl(ev.target.value)}
        placeholder="https://gateway.example.com"
        className="w-full border border-line-strong bg-surface px-1.5 py-1 font-mono text-[11px] text-ink"
      />
      {/*
        `type="password"`, and never given a value from a response. The server
        has no read path for the key, so there is nothing to prefill it with —
        which is the property, not an omission.
      */}
      <input
        type="password"
        autoComplete="new-password"
        aria-label="Endpoint API key"
        data-testid="gateway-endpoint-api-key-input"
        value={apiKey}
        onChange={(ev) => setApiKey(ev.target.value)}
        placeholder="api key (leave empty if the gateway needs none)"
        className="w-full border border-line-strong bg-surface px-1.5 py-1 text-[11px] text-ink"
      />
      <div className="flex items-center gap-1.5">
        <button
          type="submit"
          data-testid="gateway-endpoint-save"
          disabled={save.isPending || name.trim() === "" || baseUrl.trim() === ""}
          className="border border-line-strong px-2 py-1 text-[11px] font-medium text-ink-soft hover:bg-elevated disabled:opacity-50"
        >
          {save.isPending ? "Saving…" : "Save endpoint"}
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="px-1 text-[11px] text-ink-muted hover:text-ink"
        >
          Cancel
        </button>
      </div>
      {save.isError && (
        <p className="text-[11px] text-bad" data-testid="gateway-endpoint-save-error">
          {errMsg(save.error)}
        </p>
      )}
    </form>
  );
}

import { getAccessToken } from "@/lib/auth";

const baseUrl =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

type Json = Record<string, unknown> | unknown[];

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: unknown,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: Json): Promise<T> {
  const token = await getAccessToken();
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(`${baseUrl}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "omit",
  });
  if (!resp.ok) {
    let payload: unknown = null;
    try {
      payload = await resp.json();
    } catch {
      payload = await resp.text();
    }
    throw new ApiError(`request ${method} ${path} failed: ${resp.status}`, resp.status, payload);
  }
  if (resp.status === 204) {
    return undefined as unknown as T;
  }
  return (await resp.json()) as T;
}

/** Absolute API URL for browser-native loads (iframe/img src) that can't go
 * through `api.get` — e.g. the authenticated asset streaming route. */
export function apiUrl(path: string): string {
  return `${baseUrl}${path}`;
}

export const api = {
  get<T>(path: string): Promise<T> {
    return request("GET", path);
  },
  post<T>(path: string, body: Json): Promise<T> {
    return request("POST", path, body);
  },
  put<T>(path: string, body: Json): Promise<T> {
    return request("PUT", path, body);
  },
  patch<T>(path: string, body: Json): Promise<T> {
    return request("PATCH", path, body);
  },
  del<T>(path: string): Promise<T> {
    return request("DELETE", path);
  },
};

export interface ProjectOut {
  id: string;
  title: string;
  description: string;
  status: "active" | "archived" | "deleted";
  model_profile_id: string;
  created_at: string;
  updated_at: string;
  created_by: string;
}

export interface MeOut {
  user_id: string;
  subject: string;
  email: string;
  actor_kind: "user" | "aleph_agent" | "system";
}

export interface CostRollupOut {
  total_usd: string;
  by_phase: Array<{ key: string; cost_usd: string; call_count: number }>;
  by_model: Array<{ key: string; cost_usd: string; call_count: number }>;
  recent_calls: Array<{
    id: string;
    capability: string;
    model: string;
    purpose: string;
    cost_usd: string;
    timestamp: string;
  }>;
}

export interface ConnectorOut {
  id: string;
  kind: string;
  name: string;
  output_kind: string;
  requires_auth: boolean;
  enabled_by_default: boolean;
}

export interface ConnectorBindingOut {
  id: string;
  project_id: string;
  connector_id: string;
  enabled: boolean;
  config_jsonb: Record<string, unknown>;
  created_at: string;
}

export interface CredentialOut {
  connector_kind: string;
  has_project_specific: boolean;
  rotated_at: string | null;
  status: string | null;
}

export interface ModelBindingOut {
  model: string;
  provider?: string;
  max_input_tokens?: number;
  cost_per_input_token_usd?: string | number;
  cost_per_output_token_usd?: string | number;
}

export interface ModelProfileOut {
  id: string;
  name: string;
  is_template?: boolean;
  bindings: Record<string, ModelBindingOut | undefined>;
}

/**
 * A model the configured gateway actually serves.
 *
 * There is no committed model list in the client either — the options come
 * from `/v1/gateway/models`, which reads the gateway's own `/model/info`.
 * `capabilities` is computed server-side by the same policy that picks
 * defaults, so the picker cannot offer a model that would fail at runtime.
 */
export interface AutoconfigureOut {
  profile: ModelProfileOut;
  /** capability → chosen model id. */
  bound: Record<string, string>;
  /** Capabilities no available model qualified for; left unbound on purpose. */
  unbound: string[];
  /** Advertised models that failed a real invocation, mapped to the error. */
  unreachable: Record<string, string>;
}

export interface GatewayModelOut {
  id: string;
  mode: string | null;
  max_input_tokens: number | null;
  input_per_token: string | null;
  output_per_token: string | null;
  supports_vision: boolean;
  supports_function_calling: boolean;
  supports_reasoning: boolean;
  supports_prompt_caching: boolean;
  is_priced: boolean;
  capabilities: string[];
}

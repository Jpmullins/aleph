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
  budget_id: string | null;
  created_at: string;
  updated_at: string;
  created_by: string;
}

export interface MeOut {
  user_id: string;
  subject: string;
  email: string;
  actor_kind: "user" | "aleph_agent" | "aiq_agent" | "system";
}

export interface CostRollupOut {
  cap_usd: string;
  spent_usd: string;
  soft_pct: string;
  hard_pct: string;
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

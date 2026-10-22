// Talks to the FastAPI backend's /console API (apps/api/routers/console.py).
// NEXT_PUBLIC_API_URL points at the backend: http://127.0.0.1:8000 locally, the
// public server URL once this app is deployed (e.g. on Vercel).

export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
// The browser demo (talk to the agent through the mic, no phone call) is served by the backend.
export const DEMO_URL = `${API_URL}/demo`;
const TOKEN_KEY = "console_token";

export type Result =
  | "Interested"
  | "Callback requested"
  | "Not interested"
  | "Do not call"
  | "Unclear"
  | "No answer"
  | "Busy"
  | "Call failed"
  | "Canceled"
  | "Not connected"
  | "Not called yet"
  | "Calling…";

export interface CallRow {
  attempt_id: string;
  lead_id: string;
  time: string | null;
  name: string;
  business: string;
  phone: string;
  result: Result;
  outcome: string;
  status: string;
  talk_seconds: number | null;
  final_state: string;
  path: string;
  callback_time: string;
  summary: string;
  campaign: string;
}

export interface CallDetail extends CallRow {
  facts: string;
  transcript: { speaker: "agent" | "customer"; text: string }[];
}

export interface LiveCall {
  attempt_id: string;
  name: string;
  phone: string;
  business: string;
  status: "ringing" | "talking";
  seconds: number;
  state: string | null;
  lines: { speaker: "agent" | "customer"; text: string }[];
  test: boolean;
}

export interface Overview {
  campaign: { status: string; call_window_start: string; call_window_end: string; timezone: string };
  within_calling_hours: boolean;
  max_call_seconds: number;
  leads: { total: number; pending: number; by_status: Record<string, number> };
  results: Partial<Record<Result, number>>;
  calls_total: number;
  calls_today: number;
  talk_seconds: number;
  live: LiveCall | null;
  recent: CallRow[];
}

export interface Lead {
  id: string;
  name: string;
  business: string;
  phone: string;
  status: string;
  attempts: number;
  result: Result;
  last_call_id: string | null;
  summary: string;
  added: string;
  category: string;
}

export interface SystemStatus {
  telephony: string;
  ai_backend: string;
  llm: string;
  public_url_ok: boolean;
  balance: number | null;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode: stays logged in for this tab only */
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
  let resp: Response;
  try {
    resp = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the server. Is the backend running?");
  }
  if (resp.status === 401 && path !== "/console/login") {
    setToken(null);
    if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      window.location.href = "/login"; // eslint-disable-line @next/next/no-location-assign-relative-destination -- outside React; a full reload drops stale state
    }
  }
  if (!resp.ok) {
    let message = resp.statusText;
    try {
      const body = await resp.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(resp.status, message);
  }
  const type = resp.headers.get("content-type") || "";
  return (type.includes("application/json") ? resp.json() : resp.blob()) as Promise<T>;
}

export const api = {
  login: (username: string, password: string) =>
    request<{ token: string }>("/console/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  overview: () => request<Overview>("/console/overview"),
  leads: () => request<Lead[]>("/console/leads"),
  addLead: (lead: { name: string; phone: string; business: string; category: string }) =>
    request<{ id: string; updated: boolean }>("/console/leads", { method: "POST", body: JSON.stringify(lead) }),
  deleteLead: (id: string) => request<{ deleted: boolean }>(`/console/leads/${id}`, { method: "DELETE" }),
  uploadLeads: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<{
      rows: number;
      valid: number;
      created: number;
      skipped_existing: number;
      skipped_suppressed: number;
      duplicates_in_file: number;
      errors: { row: number; reason: string }[];
    }>("/console/leads/upload", { method: "POST", body: form });
  },
  testCall: (body: { phone: string; name: string; business: string; category: string }) =>
    request<{ attempt_id: string; status: string }>("/console/calls/test", { method: "POST", body: JSON.stringify(body) }),
  calls: () => request<CallRow[]>("/console/calls"),
  hangup: (id: string) => request<{ ended: boolean }>(`/console/calls/${id}/hangup`, { method: "POST" }),
  call: (id: string) => request<CallDetail>(`/console/calls/${id}`),
  start: () => request<{ status: string }>("/console/campaign/start", { method: "POST" }),
  pause: () => request<{ status: string }>("/console/campaign/pause", { method: "POST" }),
  setHours: (start: string, end: string) =>
    request<{ start: string; end: string }>("/console/campaign/hours", {
      method: "PUT",
      body: JSON.stringify({ start, end }),
    }),
  system: () => request<SystemStatus>("/console/system"),
};

export async function download(path: string, filename: string) {
  const blob = await request<Blob>(path);
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m ? `${m}m ${s.toString().padStart(2, "0")}s` : `${s}s`;
}

export function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

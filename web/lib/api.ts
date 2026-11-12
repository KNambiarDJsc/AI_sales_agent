// Talks to the FastAPI backend's /console API (apps/api/routers/console.py and
// console_admin.py). The backend's address is asked from this app's own /api/config at
// runtime (app/api/config/route.ts): it can change while the page is open (the backend
// sits behind a tunnel), so a request that can't connect asks again and retries once.

const TOKEN_KEY = "console_token";
let apiUrl: string | null = null;

export async function getApiUrl(force = false): Promise<string> {
  if (apiUrl && !force) return apiUrl;
  try {
    const resp = await fetch("/api/config", { cache: "no-store" });
    apiUrl = String((await resp.json()).apiUrl).replace(/\/$/, "");
  } catch {
    apiUrl = apiUrl ?? "http://127.0.0.1:8000";
  }
  return apiUrl;
}

// The browser demo (talk to the agent through the mic, no phone call) is served by the
// backend. It spends AI credit, so it needs the console's login token.
export async function demoUrl(params: Record<string, string> = {}): Promise<string> {
  const query = new URLSearchParams({ ...params, token: getToken() ?? "" });
  return `${await getApiUrl()}/demo?${query}`;
}

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

export interface CostParts {
  phone: number;
  listening: number;
  voice: number;
  brain: number;
}

export interface CallRow {
  cost: number | null; // approx. rupees
  cost_parts: CostParts | null;
  cost_phone_actual: boolean;
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
  transcript_lines?: number; // list endpoint only
}

export interface CallDetail extends CallRow {
  seconds: number;
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
  script: { id: string; name: string; language_style: string; agent_name: string };
  dialer: { max_attempts: number; retry_gap_minutes: number; retry_waiting: number };
  problems: string[];
  cost: { total: number; today: number; avg_per_call: number | null; per_interested: number | null };
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
  speech: string;
  public_url_ok: boolean;
  public_url_reachable: boolean;
  balance: number | null;
  problems: string[];
  max_call_seconds: number;
}

export type LanguageStyle = "hinglish" | "hindi" | "english";

export interface ScriptSummary {
  id: string;
  name: string;
  description: string;
  language_style: LanguageStyle;
  agent_name: string;
  company_name: string;
  source: "builtin" | "uploaded" | "written";
  source_filename: string;
  active: boolean;
  builtin: boolean;
  edited: boolean;
  chars: number;
  opening_line: string;
}

export interface ScriptContent {
  name: string;
  description: string;
  language_style: LanguageStyle;
  agent_name: string;
  company_name: string;
  opening_lines: string[];
  script_text: string;
  knowledge: Record<string, string>;
  objection_playbook: Record<string, string>;
  product_info: string;
  target_customer: string;
  source: string;
  source_filename: string;
}

export interface ScriptFull extends ScriptContent {
  id: string;
  active: boolean;
  builtin: boolean;
  edited: boolean;
}

export interface ScriptList {
  active_id: string;
  scripts: ScriptSummary[];
  fields: string[];
  lead_fields: string[];
  max_chars: number;
  file_types: string[];
}

export interface ScriptDraft {
  draft: ScriptContent;
  organised: boolean; // false: the AI couldn't sort it; the draft is the document as-is
  original_text: string;
  original_chars: number;
}

export interface Money {
  amount: number;
  currency: "INR" | "USD";
  inr: number;
}

export interface Account {
  id: string;
  name: string;
  role: string;
  does: string;
  help: string;
  required_for_calls: boolean;
  custom: boolean;
  currency: "INR" | "USD";
  balance_kind: "live" | "estimated" | "manual" | "none";
  balance_source: "api" | "tracked" | "manual";
  balance: Money | null;
  spent_since: Money | null;
  balance_as_of: string | null;
  recharge_url: string;
  dashboard_url: string;
  per_call: number;
  calls_left: number | null;
  low: boolean;
  in_use: boolean;
  linked: boolean;
  source: "console" | "server" | "";
  needs_relink: boolean;
  can_use_server_keys: boolean;
  fields: { field: string; label: string; secret: boolean; value: string; set: boolean }[];
}

export interface AccountsOverview {
  usd_inr: number;
  usd_inr_default: number;
  accounts: Account[];
  stack: { telephony: string; stt: string; tts: string; llm_model: string; llm_priority: boolean };
  per_call: { seconds: number; total: number; parts: CostParts; by_provider: Record<string, number> };
  per_minute: number;
  actual: {
    calls: number;
    connected: number;
    total: number;
    avg_per_connected: number | null;
    avg_seconds: number | null;
    per_minute: number | null;
  };
  calls_left: number | null;
  problems: string[];
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
    resp = await fetch(`${await getApiUrl()}${path}`, { ...init, headers });
  } catch {
    // The backend's address may have changed (new tunnel): ask again, retry once.
    const before = apiUrl;
    const after = await getApiUrl(true);
    try {
      if (after === before) throw new Error("same address");
      resp = await fetch(`${after}${path}`, { ...init, headers });
    } catch {
      throw new ApiError(0, "Can't reach the calling server right now. It may be restarting — try again in a minute.");
    }
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
  testCall: (body: {
    phone: string;
    name: string;
    business: string;
    category: string;
    brand?: string;
    email?: string;
    script_id?: string;
  }) =>
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
  setRetries: (max_attempts: number, retry_gap_minutes: number) =>
    request<{ max_attempts: number }>("/console/campaign/retries", {
      method: "PUT",
      body: JSON.stringify({ max_attempts, retry_gap_minutes }),
    }),
  retryUnanswered: () => request<{ requeued: number }>("/console/leads/retry-unanswered", { method: "POST" }),
  clearUncalledLeads: () => request<{ deleted: number }>("/console/leads", { method: "DELETE" }),

  scripts: () => request<ScriptList>("/console/scripts"),
  script: (id: string) => request<ScriptFull>(`/console/scripts/${encodeURIComponent(id)}`),
  readScriptFile: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return request<ScriptDraft>("/console/scripts/read", { method: "POST", body: form });
  },
  readScriptText: (text: string) =>
    request<ScriptDraft>("/console/scripts/read-text", { method: "POST", body: JSON.stringify({ text }) }),
  createScript: (body: ScriptContent) =>
    request<{ id: string }>("/console/scripts", { method: "POST", body: JSON.stringify(body) }),
  updateScript: (id: string, body: ScriptContent) =>
    request<{ id: string }>(`/console/scripts/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteScript: (id: string) =>
    request<{ deleted: boolean; reset_to_original: boolean }>(`/console/scripts/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  activateScript: (id: string) =>
    request<{ active_id: string }>(`/console/scripts/${encodeURIComponent(id)}/activate`, { method: "POST" }),
  previewOpening: (body: {
    opening_lines: string[];
    agent_name: string;
    company_name: string;
    language_style: string;
    lead: Record<string, string>;
  }) => request<{ line: string; from_script: boolean }>("/console/scripts/preview-opening", { method: "POST", body: JSON.stringify(body) }),

  accounts: () => request<AccountsOverview>("/console/accounts"),
  linkAccount: (id: string, values: Record<string, string>, use_server_keys = false) =>
    request<{ linked: boolean; message: string }>(`/console/accounts/${encodeURIComponent(id)}/link`, {
      method: "POST",
      body: JSON.stringify({ values, use_server_keys }),
    }),
  unlinkAccount: (id: string) =>
    request<{ linked: boolean }>(`/console/accounts/${encodeURIComponent(id)}/unlink`, { method: "POST" }),
  updateAccount: (id: string, body: { balance?: number; recharge_url?: string; low_balance?: number }) =>
    request<{ saved: boolean }>(`/console/accounts/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify(body) }),
  addAccount: (body: { name: string; role: string; recharge_url: string; balance: number | null; currency: string }) =>
    request<{ id: string }>("/console/accounts", { method: "POST", body: JSON.stringify(body) }),
  setUsdInr: (usd_inr: number) =>
    request<{ usd_inr: number }>("/console/account-settings", { method: "PUT", body: JSON.stringify({ usd_inr }) }),
};

export function fmtInr(amount: number | null | undefined, digits = 2): string {
  if (amount == null) return "—";
  return `₹${amount.toLocaleString("en-IN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function fmtMoney(money: Money | null | undefined): string {
  if (!money) return "—";
  return money.currency === "USD" ? `$${money.amount.toFixed(2)}` : fmtInr(money.amount);
}

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

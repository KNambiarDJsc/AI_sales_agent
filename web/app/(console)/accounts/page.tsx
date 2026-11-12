"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2, ExternalLink, Link2, Link2Off, Plus, RefreshCw, Trash2, Wallet, X } from "lucide-react";
import { api, fmtInr, fmtMoney, type Account, type AccountsOverview } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { ErrorNote, PageHeader, Spinner } from "@/components/ui";

const PART_LABEL: [keyof AccountsOverview["per_call"]["parts"], string, string][] = [
  ["phone", "Phone line", "#6a4df5"],
  ["listening", "Listening (speech to text)", "#0b8aa8"],
  ["voice", "Voice (text to speech)", "#d6307f"],
  ["brain", "Brain (AI replies)", "#e2631b"],
];

export default function AccountsPage() {
  const { data, error, refresh: load } = usePoll(api.accounts, 60_000);
  const [adding, setAdding] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  if (!data) {
    return (
      <div className="flex justify-center py-24 text-haze">{error ? <ErrorNote message={error} /> : <Spinner className="h-6 w-6" />}</div>
    );
  }

  const minutes = Math.round(data.per_call.seconds / 60);
  const total = Math.max(data.per_call.total, 0.01);
  return (
    <>
      <PageHeader title="Accounts & cost" kicker="What calls run on">
        <button
          className="btn btn-ghost"
          disabled={refreshing}
          onClick={async () => {
            setRefreshing(true);
            await load();
            setRefreshing(false);
          }}
        >
          {refreshing ? <Spinner /> : <RefreshCw className="h-4 w-4" />} Refresh
        </button>
        <button className="btn btn-primary" onClick={() => setAdding(true)}>
          <Plus className="h-4 w-4" /> Add another account
        </button>
      </PageHeader>

      <div className="mb-6 space-y-3">
        <ErrorNote message={error} />
        {data.problems.map((p) => (
          <div key={p} className="flex items-start gap-2 rounded-xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-800">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> {p}
          </div>
        ))}
      </div>

      <div className="mb-6 grid gap-6 lg:grid-cols-5">
        <div className="card card-glow p-6 lg:col-span-3">
          <p className="text-xs font-semibold uppercase tracking-wider text-haze">Approx. cost of one call</p>
          <div className="mt-1 flex flex-wrap items-end gap-x-6 gap-y-1">
            <p className="font-display text-4xl font-bold tabular-nums">{fmtInr(data.per_call.total)}</p>
            <p className="pb-1 text-sm text-haze">
              for a full {minutes}-minute call · about <span className="font-semibold text-ink">{fmtInr(data.per_minute)}</span> per minute
            </p>
          </div>
          <div className="my-4 flex h-3 overflow-hidden rounded-full bg-black/5">
            {PART_LABEL.map(([key, , color]) => (
              <div key={key} style={{ width: `${(data.per_call.parts[key] / total) * 100}%`, background: color }} />
            ))}
          </div>
          <ul className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
            {PART_LABEL.map(([key, label, color]) => (
              <li key={key} className="flex items-center justify-between gap-3">
                <span className="flex items-center gap-2 text-haze">
                  <span className="h-2.5 w-2.5 rounded-full" style={{ background: color }} /> {label}
                </span>
                <span className="font-display font-semibold tabular-nums">{fmtInr(data.per_call.parts[key])}</span>
              </li>
            ))}
          </ul>
          <p className="mt-4 text-xs text-haze">
            An estimate from each provider&apos;s price list; a call that doesn&apos;t connect costs about ₹0.02. Each provider&apos;s own bill is the final figure.
          </p>
        </div>

        <div className="card space-y-3 p-6 lg:col-span-2">
          <p className="text-xs font-semibold uppercase tracking-wider text-haze">Your calls so far</p>
          <Row label="Calls placed" value={`${data.actual.calls} (${data.actual.connected} answered)`} />
          <Row label="Spent on calls" value={`~${fmtInr(data.actual.total)}`} />
          <Row label="Average per answered call" value={data.actual.avg_per_connected == null ? "—" : `~${fmtInr(data.actual.avg_per_connected)}`} />
          <Row label="Average call length" value={data.actual.avg_seconds == null ? "—" : `${data.actual.avg_seconds}s`} />
          <Row
            label="Calls your balances cover"
            value={data.calls_left == null ? "Set balances below" : `about ${data.calls_left.toLocaleString("en-IN")}`}
            strong
          />
          <UsdRate value={data.usd_inr} onSaved={load} />
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {data.accounts.map((a) => (
          <AccountCard key={a.id} account={a} usdInr={data.usd_inr} onChanged={load} />
        ))}
      </div>

      {adding && <AddAccount onClose={() => setAdding(false)} onDone={() => { setAdding(false); load(); }} />}
    </>
  );
}

function Row({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-3 text-sm">
      <span className="text-haze">{label}</span>
      <span className={`font-display tabular-nums ${strong ? "text-lg font-bold" : "font-semibold"}`}>{value}</span>
    </div>
  );
}

function UsdRate({ value, onSaved }: { value: number; onSaved: () => void }) {
  const [rate, setRate] = useState(String(value));
  const [busy, setBusy] = useState(false);
  const changed = Number(rate) !== value && Number(rate) >= 50 && Number(rate) <= 200;
  return (
    <div className="flex items-center gap-2 border-t border-ink-700 pt-3 text-xs text-haze">
      <label htmlFor="usd-rate">$1 =</label>
      <span>₹</span>
      <input id="usd-rate" className="input w-20 px-2 py-1 text-sm" inputMode="decimal" value={rate} onChange={(e) => setRate(e.target.value)} />
      <span className="flex-1">used to show OpenAI&apos;s dollar prices in rupees</span>
      {changed && (
        <button
          className="font-semibold text-ultra hover:underline"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            await api.setUsdInr(Number(rate)).catch(() => {});
            setBusy(false);
            onSaved();
          }}
        >
          Save
        </button>
      )}
    </div>
  );
}

function AccountCard({ account: a, usdInr, onChanged }: { account: Account; usdInr: number; onChanged: () => void }) {
  const [mode, setMode] = useState<"view" | "link" | "balance">("view");
  const [values, setValues] = useState<Record<string, string>>({});
  const [balance, setBalance] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  async function run(action: () => Promise<{ message?: string } | unknown>) {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const result = (await action()) as { message?: string };
      if (result?.message) setNote(result.message);
      setMode("view");
      setValues({});
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "That didn't work");
    } finally {
      setBusy(false);
    }
  }

  const status = !a.linked
    ? { text: "Not linked", cls: "bg-rose-100 text-rose-700" }
    : a.low
      ? { text: "Low balance", cls: "bg-amber-100 text-amber-800" }
      : { text: a.custom ? "Tracked" : "Linked", cls: "bg-volt/15 text-volt" };

  return (
    <div className={`card flex flex-col p-5 ${a.linked && a.low ? "border-amber-300" : ""}`}>
      <div className="mb-3 flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wider text-haze">{a.role}</p>
          <p className="font-display text-xl font-bold">{a.name}</p>
        </div>
        <span className={`shrink-0 rounded-full px-2.5 py-0.5 text-xs font-semibold ${status.cls}`}>{status.text}</span>
      </div>
      {a.does && <p className="mb-4 text-sm text-haze">{a.does}</p>}

      <div className="mb-4 rounded-xl bg-ink-950 p-4">
        <p className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-haze">
          <Wallet className="h-3.5 w-3.5" /> Balance
          {a.balance_kind === "live" && <span className="normal-case tracking-normal text-volt">· live</span>}
          {a.balance_kind === "estimated" && <span className="normal-case tracking-normal">· estimated</span>}
        </p>
        {a.balance ? (
          <>
            <p className="mt-1 font-display text-3xl font-bold tabular-nums">
              {a.balance_kind === "estimated" && "~"}
              {fmtMoney(a.balance)}
              {a.currency === "USD" && <span className="ml-2 text-base font-semibold text-haze">≈ {fmtInr(a.balance.inr, 0)}</span>}
            </p>
            <p className="mt-1 text-xs text-haze">
              {a.calls_left != null && <>Enough for about <span className="font-semibold text-ink">{a.calls_left.toLocaleString("en-IN")}</span> full calls · </>}
              {a.per_call > 0 && <>{fmtInr(a.per_call)} of each call</>}
            </p>
            {a.balance_kind === "estimated" && a.spent_since && (
              <p className="mt-1 text-xs text-haze">
                Your figure minus ~{fmtMoney(a.spent_since)} used by calls since {a.balance_as_of ? new Date(a.balance_as_of).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : "you set it"}.
              </p>
            )}
          </>
        ) : (
          <p className="mt-1 text-sm text-haze">
            {!a.linked
              ? "Link the account to see it."
              : a.balance_source === "api"
                ? "Couldn't read the balance just now."
                : `${a.name} has no way to read the balance automatically. Enter what you have and the console keeps count from there.`}
          </p>
        )}
      </div>

      {a.fields.length > 0 && mode === "view" && (
        <ul className="mb-4 space-y-1 text-xs text-haze">
          {a.fields.map((f) => (
            <li key={f.field} className="flex justify-between gap-3">
              <span>{f.label}</span>
              <span className="truncate font-mono text-ink">{f.set ? f.value : "—"}</span>
            </li>
          ))}
          {a.linked && <li className="pt-1">{a.source === "console" ? "Linked from this console." : "Using the keys set on the server."}</li>}
          {a.needs_relink && <li className="font-semibold text-amber-700">The saved keys can&apos;t be read any more — link the account again.</li>}
        </ul>
      )}

      {mode === "link" && (
        <form
          className="mb-4 space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            run(() => api.linkAccount(a.id, values));
          }}
        >
          {a.fields.map((f) => (
            <div key={f.field}>
              <label className="label" htmlFor={`${a.id}-${f.field}`}>{f.label}</label>
              <input
                id={`${a.id}-${f.field}`}
                className="input font-mono text-sm"
                type={f.secret ? "password" : "text"}
                autoComplete="off"
                required
                value={values[f.field] ?? ""}
                onChange={(e) => setValues({ ...values, [f.field]: e.target.value })}
              />
            </div>
          ))}
          {a.help && <p className="text-xs text-haze">Where to find these: {a.help}</p>}
          <p className="text-xs text-haze">The keys are checked with {a.name} first, then stored encrypted. They are never shown again.</p>
          <div className="flex flex-wrap gap-2">
            <button className="btn btn-primary" disabled={busy}>{busy ? <Spinner /> : <Link2 className="h-4 w-4" />} Check &amp; link</button>
            <button type="button" className="btn btn-ghost" onClick={() => setMode("view")}>Cancel</button>
            {a.can_use_server_keys && a.source !== "server" && (
              <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => run(() => api.linkAccount(a.id, {}, true))}>
                Use the server&apos;s keys
              </button>
            )}
          </div>
        </form>
      )}

      {mode === "balance" && (
        <form
          className="mb-4 space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            run(() => api.updateAccount(a.id, { balance: Number(balance) }));
          }}
        >
          <label className="label" htmlFor={`${a.id}-balance`}>Balance right now ({a.currency === "USD" ? "US dollars" : "rupees"})</label>
          <input id={`${a.id}-balance`} className="input" inputMode="decimal" required placeholder={a.currency === "USD" ? "5.00" : "500"} value={balance} onChange={(e) => setBalance(e.target.value)} />
          <p className="text-xs text-haze">
            Copy it from {a.name}&apos;s own page{a.dashboard_url ? " (Open dashboard below)" : ""}. Update it after every recharge
            {a.currency === "USD" ? ` — $1 is counted as ₹${usdInr}` : ""}.
          </p>
          <div className="flex gap-2">
            <button className="btn btn-primary" disabled={busy || !(Number(balance) >= 0) || balance === ""}>{busy && <Spinner />} Save</button>
            <button type="button" className="btn btn-ghost" onClick={() => setMode("view")}>Cancel</button>
          </div>
        </form>
      )}

      <ErrorNote message={error} />
      {note && <p className="mb-3 flex items-center gap-1.5 text-sm text-volt"><CheckCircle2 className="h-4 w-4" /> {note}</p>}

      {mode === "view" && (
        <div className="mt-auto flex flex-wrap gap-2 pt-1">
          {a.recharge_url && (
            <a className={`btn ${a.low || !a.balance ? "btn-volt" : "btn-primary"}`} href={a.recharge_url} target="_blank" rel="noreferrer">
              Recharge <ExternalLink className="h-4 w-4" />
            </a>
          )}
          {a.linked && a.balance_kind !== "live" && (
            <button className="btn btn-ghost" onClick={() => { setBalance(""); setMode("balance"); }}>
              {a.balance ? "Update balance" : "Set balance"}
            </button>
          )}
          {a.dashboard_url && a.dashboard_url !== a.recharge_url && (
            <a className="btn btn-ghost" href={a.dashboard_url} target="_blank" rel="noreferrer">Dashboard <ExternalLink className="h-4 w-4" /></a>
          )}
          {!a.custom && (
            <button className="btn btn-ghost" onClick={() => setMode("link")}>
              <Link2 className="h-4 w-4" /> {a.linked ? "Change keys" : "Link account"}
            </button>
          )}
          {a.linked && (
            <button
              className="btn btn-ghost text-rose-700"
              disabled={busy}
              onClick={() => {
                const warn = a.custom
                  ? `Remove “${a.name}” from this page?`
                  : a.required_for_calls
                    ? `Unlink ${a.name}? Calls will stop until it is linked again.`
                    : `Unlink ${a.name}? Calls will keep working with the backup voice, which speaks Hindi less naturally.`;
                if (window.confirm(warn)) run(() => api.unlinkAccount(a.id));
              }}
            >
              {a.custom ? <Trash2 className="h-4 w-4" /> : <Link2Off className="h-4 w-4" />} {a.custom ? "Remove" : "Unlink"}
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function AddAccount({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [form, setForm] = useState({ name: "", role: "", recharge_url: "", balance: "", currency: "INR" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 p-4" onClick={onClose}>
      <form
        className="card w-full max-w-md space-y-4 p-6"
        onClick={(e) => e.stopPropagation()}
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError(null);
          try {
            await api.addAccount({ ...form, balance: form.balance === "" ? null : Number(form.balance) });
            onDone();
          } catch (err) {
            setError(err instanceof Error ? err.message : "Could not add it");
            setBusy(false);
          }
        }}
      >
        <div className="flex items-center justify-between">
          <p className="font-display text-xl font-bold">Add another account</p>
          <button type="button" onClick={onClose} aria-label="Close" className="text-haze hover:text-ink"><X className="h-5 w-5" /></button>
        </div>
        <p className="text-sm text-haze">Any other service you pay for and want to keep an eye on here — a second phone line, SMS, a CRM. You keep its balance up to date yourself.</p>
        <div>
          <label className="label" htmlFor="a-name">Name *</label>
          <input id="a-name" className="input" required maxLength={60} value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="a-role">What it is for</label>
          <input id="a-role" className="input" maxLength={60} placeholder="SMS follow-ups" value={form.role} onChange={(e) => setForm({ ...form, role: e.target.value })} />
        </div>
        <div>
          <label className="label" htmlFor="a-url">Recharge link</label>
          <input id="a-url" className="input" type="url" placeholder="https://…" value={form.recharge_url} onChange={(e) => setForm({ ...form, recharge_url: e.target.value })} />
        </div>
        <div className="grid grid-cols-[1fr_7rem] gap-3">
          <div>
            <label className="label" htmlFor="a-balance">Balance now</label>
            <input id="a-balance" className="input" inputMode="decimal" value={form.balance} onChange={(e) => setForm({ ...form, balance: e.target.value })} />
          </div>
          <div>
            <label className="label" htmlFor="a-currency">Currency</label>
            <select id="a-currency" className="input" value={form.currency} onChange={(e) => setForm({ ...form, currency: e.target.value })}>
              <option value="INR">₹ INR</option>
              <option value="USD">$ USD</option>
            </select>
          </div>
        </div>
        <ErrorNote message={error} />
        <button className="btn btn-primary w-full" disabled={busy || !form.name.trim()}>{busy && <Spinner />} Add account</button>
      </form>
    </div>
  );
}

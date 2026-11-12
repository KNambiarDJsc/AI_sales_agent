"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Clock, Download, PhoneMissed, Server, XCircle } from "lucide-react";
import { api, download, type Overview, type SystemStatus } from "@/lib/api";
import { ErrorNote, PageHeader, Spinner } from "@/components/ui";

export default function SettingsPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [hours, setHours] = useState({ start: "", end: "" });
  const [retries, setRetries] = useState({ max_attempts: 2, retry_gap_minutes: 30 });
  const [retrySaved, setRetrySaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.overview().then((o) => {
      setOverview(o);
      setHours({ start: o.campaign.call_window_start, end: o.campaign.call_window_end });
      setRetries({ max_attempts: o.dialer.max_attempts, retry_gap_minutes: o.dialer.retry_gap_minutes });
    }).catch((e) => setError(e.message));
    api.system().then(setSystem).catch(() => {});
  }, []);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await api.setHours(hours.start, hours.end);
      setSaved(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader title="Settings" kicker="Control room" />
      <div className="grid gap-6 lg:grid-cols-2">
        <form onSubmit={save} className="card card-glow space-y-5 p-6">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-brand p-2.5"><Clock className="h-5 w-5 text-white" /></div>
            <div>
              <p className="font-display text-lg font-semibold">Calling hours</p>
              <p className="text-xs text-haze">Leads are only called in this window ({overview?.campaign.timezone ?? "…"})</p>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">From</label>
              <input type="time" className="input" value={hours.start} onChange={(e) => setHours({ ...hours, start: e.target.value })} />
            </div>
            <div>
              <label className="label">Until</label>
              <input type="time" className="input" value={hours.end} onChange={(e) => setHours({ ...hours, end: e.target.value })} />
            </div>
          </div>
          <ErrorNote message={error} />
          {saved && <p className="text-sm text-volt">Saved.</p>}
          <button className="btn btn-primary" disabled={busy || !hours.start || !hours.end}>
            {busy && <Spinner />} Save hours
          </button>
        </form>

        <form
          className="card space-y-5 p-6"
          onSubmit={async (e) => {
            e.preventDefault();
            setRetrySaved(false);
            try {
              await api.setRetries(retries.max_attempts, retries.retry_gap_minutes);
              setRetrySaved(true);
            } catch (err) {
              setError(err instanceof Error ? err.message : "Could not save");
            }
          }}
        >
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-flame/15 p-2.5"><PhoneMissed className="h-5 w-5 text-flame" /></div>
            <div>
              <p className="font-display text-lg font-semibold">When nobody answers</p>
              <p className="text-xs text-haze">Leads who didn&apos;t pick up (or were busy) are called again automatically.</p>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label" htmlFor="tries">Tries per lead</label>
              <select id="tries" className="input" value={retries.max_attempts} onChange={(e) => setRetries({ ...retries, max_attempts: Number(e.target.value) })}>
                {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{n === 1 ? "1 (no retry)" : n}</option>)}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="gap">Wait before retrying</label>
              <select id="gap" className="input" value={retries.retry_gap_minutes} onChange={(e) => setRetries({ ...retries, retry_gap_minutes: Number(e.target.value) })}>
                {[10, 30, 60, 120, 240].map((n) => <option key={n} value={n}>{n < 60 ? `${n} minutes` : `${n / 60} hour${n > 60 ? "s" : ""}`}</option>)}
              </select>
            </div>
          </div>
          <p className="text-xs text-haze">A lead who talked to the agent, asked for a call back or asked not to be called is never called again by this.</p>
          {retrySaved && <p className="text-sm text-volt">Saved.</p>}
          <button className="btn btn-primary">Save</button>
        </form>

        <div className="card space-y-4 p-6">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-cyanx/20 p-2.5"><Server className="h-5 w-5 text-cyanx" /></div>
            <p className="font-display text-lg font-semibold">System</p>
          </div>
          {!system ? (
            <Spinner />
          ) : (
            <ul className="space-y-2.5 text-sm">
              <Check ok={system.problems.length === 0} label="Ready to call" value={system.problems[0] ?? "Yes"} />
              <Check ok label="Phone line" value={system.telephony} />
              <Check ok label="Voice & listening" value={system.speech === "sarvam" ? "Sarvam AI (Indian voice)" : "OpenAI (backup voice)"} />
              <Check ok label="Brain" value={system.llm} />
              <Check ok label="Longest call" value={`${Math.round(system.max_call_seconds / 60)} minutes`} />
              <Check ok={system.public_url_reachable} label="Phone company can reach us" value={system.public_url_reachable ? "Yes" : "Not right now — it reconnects by itself within a minute"} />
              <Check
                ok={system.balance == null || system.balance > 5}
                label="Call balance"
                value={system.balance == null ? "—" : `₹${system.balance.toFixed(2)}`}
              />
            </ul>
          )}
        </div>

        <div className="card flex flex-wrap items-center gap-4 p-6 lg:col-span-2">
          <div className="mr-auto">
            <p className="font-display text-lg font-semibold">Export everything</p>
            <p className="text-sm text-haze">One Excel file: all leads with their latest result, every call, and the interested leads.</p>
          </div>
          <button className="btn btn-volt" onClick={() => download("/console/export.xlsx", "leads-results.xlsx")}>
            <Download className="h-4 w-4" /> Download Excel
          </button>
        </div>
      </div>
    </>
  );
}

function Check({ ok, label, value }: { ok: boolean; label: string; value: string }) {
  return (
    <li className="flex items-center gap-3">
      {ok ? <CheckCircle2 className="h-4 w-4 text-volt" /> : <XCircle className="h-4 w-4 text-amber-400" />}
      <span className="w-36 text-haze">{label}</span>
      <span className="min-w-0 flex-1 truncate font-medium">{value}</span>
    </li>
  );
}

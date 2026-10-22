"use client";

import { useEffect, useState } from "react";
import { CheckCircle2, Clock, Download, Server, XCircle } from "lucide-react";
import { API_URL, api, download, type Overview, type SystemStatus } from "@/lib/api";
import { ErrorNote, PageHeader, Spinner } from "@/components/ui";

export default function SettingsPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [system, setSystem] = useState<SystemStatus | null>(null);
  const [hours, setHours] = useState({ start: "", end: "" });
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.overview().then((o) => {
      setOverview(o);
      setHours({ start: o.campaign.call_window_start, end: o.campaign.call_window_end });
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

        <div className="card space-y-4 p-6">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-cyanx/20 p-2.5"><Server className="h-5 w-5 text-cyanx" /></div>
            <p className="font-display text-lg font-semibold">System</p>
          </div>
          {!system ? (
            <Spinner />
          ) : (
            <ul className="space-y-2.5 text-sm">
              <Check ok label="Server" value={API_URL} />
              <Check ok label="Phone provider" value={system.telephony} />
              <Check ok label="AI" value={`${system.ai_backend} · ${system.llm}`} />
              <Check ok={system.public_url_ok} label="Public call URL" value={system.public_url_ok ? "Reachable over https" : "Not set: calls can't connect"} />
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

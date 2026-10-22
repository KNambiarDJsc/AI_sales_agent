"use client";

import { useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  CheckCircle2,
  FileSpreadsheet,
  Phone,
  Plus,
  Search,
  Trash2,
  UploadCloud,
  Users,
} from "lucide-react";
import { api, download, fmtTime, type Lead } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { Empty, ErrorNote, PageHeader, ResultChip, Spinner } from "@/components/ui";

type UploadReport = Awaited<ReturnType<typeof api.uploadLeads>>;

const FILTERS = ["All", "Not called yet", "Interested", "Callback requested", "Not interested", "Other"] as const;

export default function LeadsPage() {
  const { data: leads, error, refresh } = usePoll(api.leads, 5000);
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("All");
  const [query, setQuery] = useState("");

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (leads ?? []).filter((l) => {
      const named = ["Not called yet", "Interested", "Callback requested", "Not interested"];
      if (filter === "Other" ? named.includes(l.result) : filter !== "All" && l.result !== filter) return false;
      return !q || `${l.name} ${l.business} ${l.phone}`.toLowerCase().includes(q);
    });
  }, [leads, filter, query]);

  return (
    <>
      <PageHeader title="Leads" kicker="Who the agent calls">
        <button className="btn btn-ghost" onClick={() => download("/console/leads/template.xlsx", "leads-template.xlsx")}>
          <FileSpreadsheet className="h-4 w-4" /> Excel template
        </button>
        <Link href="/" className="btn btn-volt">Go to Start</Link>
      </PageHeader>

      <div className="mb-6 grid gap-6 lg:grid-cols-5">
        <Uploader onDone={refresh} />
        <AddLead onDone={refresh} />
      </div>

      <div className="card p-5">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <h2 className="mr-auto font-display text-lg font-semibold">
            All leads <span className="text-haze">({leads?.length ?? 0})</span>
          </h2>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-haze" />
            <input className="input w-56 py-2 pl-9" placeholder="Search" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
        </div>
        <div className="mb-4 flex flex-wrap gap-2">
          {FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`rounded-full px-3 py-1 text-xs font-semibold transition ${
                filter === f ? "bg-brand text-white" : "bg-white/5 text-haze hover:bg-white/10"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <ErrorNote message={error} />
        {!leads ? (
          <div className="flex justify-center py-10 text-haze"><Spinner className="h-6 w-6" /></div>
        ) : shown.length === 0 ? (
          <Empty
            icon={<Users className="h-6 w-6" />}
            title={leads.length ? "No leads match" : "No leads yet"}
            hint={leads.length ? undefined : "Upload an Excel sheet or add one by hand above."}
          />
        ) : (
          <div className="-mx-5 overflow-x-auto">
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead className="text-xs uppercase tracking-wider text-haze">
                <tr>
                  <th className="px-5 py-2 font-semibold">Lead</th>
                  <th className="px-3 py-2 font-semibold">Phone</th>
                  <th className="px-3 py-2 font-semibold">Result</th>
                  <th className="px-3 py-2 font-semibold">Calls</th>
                  <th className="px-3 py-2 font-semibold">Added</th>
                  <th className="px-5 py-2" />
                </tr>
              </thead>
              <tbody>
                {shown.map((l) => (
                  <LeadRow key={l.id} lead={l} onChange={refresh} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

function LeadRow({ lead, onChange }: { lead: Lead; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const canDelete = lead.attempts === 0 && lead.status === "pending";
  return (
    <tr className="table-row">
      <td className="px-5 py-3">
        <p className="font-medium">{lead.name || "Unnamed"}</p>
        <p className="text-xs text-haze">
          {lead.business || "—"}
          {lead.category && <span className="ml-1.5 rounded bg-ultra/20 px-1.5 py-px text-[10px] text-violet-200">{lead.category}</span>}
        </p>
      </td>
      <td className="px-3 py-3 font-mono text-xs text-zinc-300">{lead.phone}</td>
      <td className="px-3 py-3">
        <ResultChip result={lead.result} />
        {lead.summary && <p className="mt-1 max-w-xs truncate text-xs text-haze" title={lead.summary}>{lead.summary}</p>}
      </td>
      <td className="px-3 py-3 tabular-nums text-haze">{lead.attempts}</td>
      <td className="px-3 py-3 text-xs text-haze">{fmtTime(lead.added)}</td>
      <td className="px-5 py-3">
        <div className="flex justify-end gap-2">
          {lead.last_call_id && (
            <Link href={`/calls/${lead.last_call_id}`} className="btn btn-ghost px-3 py-1.5 text-xs">Transcript</Link>
          )}
          <Link
            href={`/test-call?phone=${encodeURIComponent(lead.phone)}&name=${encodeURIComponent(lead.name)}&business=${encodeURIComponent(lead.business)}&category=${encodeURIComponent(lead.category)}`}
            className="btn btn-ghost px-3 py-1.5 text-xs"
            title="Call this lead now"
          >
            <Phone className="h-3.5 w-3.5" /> Call now
          </Link>
          {canDelete && (
            <button
              className="btn btn-danger px-2.5 py-1.5 text-xs"
              disabled={busy}
              title="Remove lead"
              onClick={async () => {
                setBusy(true);
                try {
                  await api.deleteLead(lead.id);
                  onChange();
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? <Spinner className="h-3.5 w-3.5" /> : <Trash2 className="h-3.5 w-3.5" />}
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

function Uploader({ onDone }: { onDone: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<UploadReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function upload(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      setReport(await api.uploadLeads(file));
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Upload failed");
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  return (
    <div className="card card-glow p-5 lg:col-span-3">
      <h2 className="mb-1 font-display text-lg font-semibold">Upload a lead list</h2>
      <p className="mb-4 text-sm text-haze">Excel (.xlsx) or CSV with a Phone column. Name, Business and Product category are optional but make the pitch personal.</p>
      <button
        type="button"
        onClick={() => input.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          upload(e.dataTransfer.files[0]);
        }}
        className={`flex w-full flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed px-4 py-10 transition ${
          drag ? "border-hotpink bg-hotpink/10" : "border-white/12 hover:border-ultra/60 hover:bg-white/[0.02]"
        }`}
      >
        {busy ? <Spinner className="h-8 w-8 text-hotpink" /> : <UploadCloud className="h-9 w-9 text-hotpink" />}
        <span className="font-medium">{busy ? "Importing…" : "Drop your Excel here or click to browse"}</span>
        <span className="text-xs text-haze">Max 5 MB · duplicates and do-not-call numbers are skipped</span>
      </button>
      <input ref={input} type="file" accept=".xlsx,.csv" className="hidden" onChange={(e) => upload(e.target.files?.[0])} />
      <div className="mt-4 space-y-3">
        <ErrorNote message={error} />
        {report && (
          <div className="rounded-xl border border-volt/30 bg-volt/10 p-4 text-sm">
            <p className="flex items-center gap-2 font-semibold text-volt">
              <CheckCircle2 className="h-4 w-4" /> {report.created} new lead{report.created === 1 ? "" : "s"} added
            </p>
            <p className="mt-1 text-lime-100/80">
              {report.rows} rows read
              {report.skipped_existing ? ` · ${report.skipped_existing} already in the list` : ""}
              {report.duplicates_in_file ? ` · ${report.duplicates_in_file} duplicates in the file` : ""}
              {report.skipped_suppressed ? ` · ${report.skipped_suppressed} on do-not-call` : ""}
              {report.errors.length ? ` · ${report.errors.length} rows with problems` : ""}
            </p>
            {report.errors.length > 0 && (
              <ul className="mt-2 max-h-28 space-y-0.5 overflow-y-auto text-xs text-rose-200">
                {report.errors.map((e) => (
                  <li key={e.row}>Row {e.row}: {e.reason}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function AddLead({ onDone }: { onDone: () => void }) {
  const [form, setForm] = useState({ name: "", phone: "", business: "", category: "" });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setSaved(null);
    try {
      const r = await api.addLead(form);
      setSaved(r.updated ? "Lead updated" : "Lead added. It will be called when calling is on.");
      setForm({ name: "", phone: "", business: "", category: "" });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add lead");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="card space-y-4 p-5 lg:col-span-2">
      <h2 className="font-display text-lg font-semibold">Add one lead</h2>
      <div>
        <label className="label">Phone *</label>
        <input className="input" required placeholder="98765 43210" value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className="label">Name</label>
          <input className="input" placeholder="Priya" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </div>
        <div>
          <label className="label">Business</label>
          <input className="input" placeholder="Priya Crafts" value={form.business} onChange={(e) => setForm({ ...form, business: e.target.value })} />
        </div>
      </div>
      <div>
        <label className="label">Product category</label>
        <input className="input" placeholder="Handmade jewellery" value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} />
      </div>
      <ErrorNote message={error} />
      {saved && <p className="text-sm text-volt">{saved}</p>}
      <button className="btn btn-primary w-full" disabled={busy || !form.phone}>
        {busy ? <Spinner /> : <Plus className="h-4 w-4" />} Add to list
      </button>
    </form>
  );
}

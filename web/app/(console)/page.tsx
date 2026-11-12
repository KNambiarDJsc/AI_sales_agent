"use client";

import { useState } from "react";
import Link from "next/link";
import {
  ArrowUpRight,
  CalendarClock,
  Clock,
  Download,
  FileText,
  Flame,
  IndianRupee,
  Pause,
  PhoneCall,
  Play,
  Sparkles,
  Users,
} from "lucide-react";
import { api, download, fmtDuration, fmtInr, fmtTime, type Result } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { LiveCallCard } from "@/components/LiveCall";
import { Empty, ErrorNote, PageHeader, ResultChip, Spinner } from "@/components/ui";

const FUNNEL: { key: Result; color: string }[] = [
  { key: "Interested", color: "#15924a" },
  { key: "Callback requested", color: "#d99a06" },
  { key: "Not interested", color: "#fb7185" },
  { key: "Do not call", color: "#ef4444" },
  { key: "No answer", color: "#0b8aa8" },
];

export default function Dashboard() {
  const { data, error, refresh } = usePoll(api.overview, 3000);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  async function toggle() {
    if (!data) return;
    setBusy(true);
    setActionError(null);
    try {
      await (data.campaign.status === "active" ? api.pause() : api.start());
      await refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : "Failed");
    } finally {
      setBusy(false);
    }
  }

  if (!data) {
    return (
      <div className="flex justify-center py-24 text-haze">
        {error ? <ErrorNote message={error} /> : <Spinner className="h-6 w-6" />}
      </div>
    );
  }

  const running = data.campaign.status === "active";
  const interested = data.results["Interested"] ?? 0;
  const connected = FUNNEL.slice(0, 4).reduce((n, f) => n + (data.results[f.key] ?? 0), 0);
  const total = Math.max(1, FUNNEL.reduce((n, f) => n + (data.results[f.key] ?? 0), 0));

  return (
    <>
      <PageHeader title="Dashboard" kicker="Command center">
        <button className="btn btn-ghost" onClick={() => download("/console/export.xlsx", "leads-results.xlsx")}>
          <Download className="h-4 w-4" /> Export Excel
        </button>
        <button className={`btn ${running ? "btn-danger" : "btn-volt"}`} onClick={toggle} disabled={busy}>
          {busy ? <Spinner /> : running ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
          {running ? "Pause calling" : "Start calling leads"}
        </button>
      </PageHeader>

      <div className="mb-6 space-y-3">
        <ErrorNote message={actionError} />
        {data.problems.map((p) => (
          <ErrorNote key={p} message={p} />
        ))}
        <StatusBanner
          running={running}
          pending={data.leads.pending}
          retryWaiting={data.dialer.retry_waiting}
          retryGap={data.dialer.retry_gap_minutes}
          inHours={data.within_calling_hours}
          window={`${data.campaign.call_window_start}–${data.campaign.call_window_end}`}
        />
      </div>

      <div className="mb-6 grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat icon={<Users />} label="Leads" value={data.leads.total} sub={`${data.leads.pending} waiting`} tint="from-ultra/10" />
        <Stat icon={<PhoneCall />} label="Calls today" value={data.calls_today} sub={`${data.calls_total} all time`} tint="from-hotpink/10" />
        <Stat
          icon={<Flame />}
          label="Interested"
          value={interested}
          sub={connected ? `${Math.round((interested / connected) * 100)}% of conversations` : "No conversations yet"}
          tint="from-volt/10"
          accent
        />
        <Stat icon={<Clock />} label="Talk time" value={fmtDuration(data.talk_seconds)} sub="Across all calls" tint="from-flame/10" />
      </div>

      <div className="mb-6 grid gap-4 lg:grid-cols-3">
        <Link href="/scripts" className="card flex items-center gap-4 p-5 transition hover:border-ultra/50">
          <div className="rounded-xl bg-ultra/10 p-3"><FileText className="h-5 w-5 text-ultra" /></div>
          <div className="min-w-0 flex-1">
            <p className="text-xs font-semibold uppercase tracking-wider text-haze">Script in use</p>
            <p className="truncate font-display text-lg font-semibold">{data.script.name}</p>
            <p className="text-xs text-haze">
              {data.script.language_style}
              {data.script.agent_name ? ` · agent ${data.script.agent_name}` : ""} · change or upload your own
            </p>
          </div>
          <ArrowUpRight className="h-4 w-4 shrink-0 text-haze" />
        </Link>
        <Link href="/accounts" className="card flex items-center gap-4 p-5 transition hover:border-ultra/50 lg:col-span-2">
          <div className="rounded-xl bg-volt/10 p-3"><IndianRupee className="h-5 w-5 text-volt" /></div>
          <div className="grid flex-1 grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
            <Money label="Spent today" value={`~${fmtInr(data.cost.today)}`} />
            <Money label="Spent in all" value={`~${fmtInr(data.cost.total)}`} />
            <Money label="Per answered call" value={data.cost.avg_per_call == null ? "—" : `~${fmtInr(data.cost.avg_per_call)}`} />
            <Money label="Per interested lead" value={data.cost.per_interested == null ? "—" : `~${fmtInr(data.cost.per_interested)}`} />
          </div>
          <ArrowUpRight className="h-4 w-4 shrink-0 text-haze" />
        </Link>
      </div>

      <div className="grid gap-6 lg:grid-cols-5">
        <div className="space-y-6 lg:col-span-3">
          {data.live ? (
            <LiveCallCard live={data.live} limit={data.max_call_seconds} onEnded={refresh} />
          ) : (
            <div className="card flex flex-col items-center justify-center gap-3 p-10 text-center">
              <div className="rounded-2xl bg-brand/20 p-4">
                <Sparkles className="h-7 w-7 text-ultra" />
              </div>
              <p className="font-display text-xl font-semibold">No call in progress</p>
              <p className="max-w-sm text-sm text-haze">
                {running
                  ? "The agent will dial the next waiting lead in a few seconds."
                  : "Upload leads and hit Start, or place a test call to hear the agent yourself."}
              </p>
              <div className="mt-2 flex gap-2">
                <Link href="/leads" className="btn btn-ghost">Add leads</Link>
                <Link href="/test-call" className="btn btn-primary">Test call</Link>
              </div>
            </div>
          )}

          <div className="card p-5">
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-display text-lg font-semibold">Recent calls</h2>
              <Link href="/calls" className="flex items-center gap-1 text-sm text-ultra hover:underline">
                All calls <ArrowUpRight className="h-4 w-4" />
              </Link>
            </div>
            {data.recent.length === 0 ? (
              <Empty icon={<PhoneCall className="h-6 w-6" />} title="No calls yet" />
            ) : (
              <ul className="divide-y divide-black/5">
                {data.recent.map((c) => (
                  <li key={c.attempt_id}>
                    <Link href={`/calls/${c.attempt_id}`} className="flex items-center gap-3 rounded-lg px-2 py-3 hover:bg-black/[0.03]">
                      <div className="min-w-0 flex-1">
                        <p className="truncate font-medium">
                          {c.name || "Unnamed"} <span className="text-haze">· {c.business || "—"}</span>
                        </p>
                        <p className="truncate text-xs text-haze">{c.summary || c.phone}</p>
                      </div>
                      <div className="hidden text-right text-xs text-haze sm:block">
                        <p>{fmtTime(c.time)}</p>
                        <p>{fmtDuration(c.talk_seconds)}</p>
                      </div>
                      <ResultChip result={c.result} />
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        <div className="space-y-6 lg:col-span-2">
          <div className="card p-5">
            <h2 className="mb-4 font-display text-lg font-semibold">Outcomes</h2>
            <div className="mb-5 flex h-3 overflow-hidden rounded-full bg-black/5">
              {FUNNEL.map((f) => {
                const n = data.results[f.key] ?? 0;
                return n ? <div key={f.key} style={{ width: `${(n / total) * 100}%`, background: f.color }} /> : null;
              })}
            </div>
            <ul className="space-y-2.5">
              {FUNNEL.map((f) => (
                <li key={f.key} className="flex items-center justify-between text-sm">
                  <span className="flex items-center gap-2 text-zinc-700">
                    <span className="h-2.5 w-2.5 rounded-full" style={{ background: f.color }} />
                    {f.key}
                  </span>
                  <span className="font-display font-semibold tabular-nums">{data.results[f.key] ?? 0}</span>
                </li>
              ))}
            </ul>
          </div>

          <div className="card p-5">
            <h2 className="mb-1 font-display text-lg font-semibold">How it works</h2>
            <ol className="mt-3 space-y-3 text-sm text-haze">
              {[
                ["Script", "Use a built-in script or upload your own (Word, PDF or text) on the Scripts page."],
                ["Upload", "Drop an Excel of leads (Name, Phone, Business) on the Leads page."],
                ["Start", "Hit Start. The agent calls them one by one during calling hours."],
                ["Collect", "Interested leads and callback requests land here and in the Excel export."],
              ].map(([t, d], i) => (
                <li key={t} className="flex gap-3">
                  <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-brand text-xs font-bold text-white">
                    {i + 1}
                  </span>
                  <span>
                    <span className="font-semibold text-ink">{t}.</span> {d}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
    </>
  );
}

function Money({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <p className="text-xs text-haze">{label}</p>
      <p className="font-display text-lg font-semibold tabular-nums">{value}</p>
    </div>
  );
}

function StatusBanner({
  running,
  pending,
  retryWaiting,
  retryGap,
  inHours,
  window,
}: {
  running: boolean;
  pending: number;
  retryWaiting: number;
  retryGap: number;
  inHours: boolean;
  window: string;
}) {
  let text: string;
  let tone: string;
  if (!running) {
    text = pending ? `Paused · ${pending} lead${pending === 1 ? "" : "s"} waiting to be called` : "Paused · add leads to start calling";
    tone = "border-black/10 bg-black/[0.03] text-haze";
  } else if (!inHours) {
    text = `Running, but outside calling hours (${window}). Calls resume automatically.`;
    tone = "border-amber-400/30 bg-amber-400/10 text-amber-700";
  } else if (!pending) {
    text = retryWaiting
      ? `Running · every lead has been tried. ${retryWaiting} didn't answer and will be called again (${retryGap} min after their last try).`
      : "Running · every lead has been called. New leads you add will be called automatically.";
    tone = "border-cyanx/30 bg-cyanx/10 text-cyan-800";
  } else {
    text = `Calling · ${pending} lead${pending === 1 ? "" : "s"} left, one at a time`;
    tone = "border-volt/30 bg-volt/10 text-green-800";
  }
  return (
    <div className={`flex items-center gap-2 rounded-xl border px-4 py-3 text-sm ${tone}`}>
      <CalendarClock className="h-4 w-4 shrink-0" /> {text}
    </div>
  );
}

function Stat({
  icon,
  label,
  value,
  sub,
  tint,
  accent,
}: {
  icon: React.ReactNode;
  label: string;
  value: React.ReactNode;
  sub: string;
  tint: string;
  accent?: boolean;
}) {
  return (
    <div className={`card overflow-hidden p-5 bg-gradient-to-br ${tint} to-transparent`}>
      <div className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-haze [&_svg]:h-4 [&_svg]:w-4">
        {icon} {label}
      </div>
      <p className={`font-display text-3xl font-bold tabular-nums sm:text-4xl ${accent ? "text-volt" : ""}`}>{value}</p>
      <p className="mt-1 truncate text-xs text-haze">{sub}</p>
    </div>
  );
}

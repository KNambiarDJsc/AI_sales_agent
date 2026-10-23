"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Download, FileText, History, Search } from "lucide-react";
import { api, download, fmtDuration, fmtTime, type CallRow } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { Empty, ErrorNote, PageHeader, ResultChip, Spinner } from "@/components/ui";

// Result filters, in the order the client cares about them.
const FILTERS = ["All", "Interested", "Callback requested", "Not interested", "Do not call", "Unclear", "No answer"];

export default function CallsPage() {
  const router = useRouter();
  const { data: calls, error } = usePoll(api.calls, 5000);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("All");

  const counts = useMemo(() => {
    const c: Record<string, number> = { All: calls?.length ?? 0 };
    for (const call of calls ?? []) c[call.result] = (c[call.result] ?? 0) + 1;
    return c;
  }, [calls]);

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (calls ?? []).filter(
      (c) =>
        (filter === "All" || c.result === filter) &&
        (!q || [c.name, c.business, c.phone].some((v) => (v || "").toLowerCase().includes(q))),
    );
  }, [calls, query, filter]);

  return (
    <>
      <PageHeader title="Call history" kicker="Every conversation, word for word">
        <button className="btn btn-ghost" onClick={() => download("/console/export.xlsx", "leads-results.xlsx")}>
          <Download className="h-4 w-4" /> Export Excel
        </button>
      </PageHeader>
      <ErrorNote message={error} />

      {calls && calls.length > 0 && (
        <div className="mb-5 flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap gap-2">
            {FILTERS.filter((f) => f === "All" || counts[f]).map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`rounded-full border px-3 py-1 text-xs font-semibold transition ${
                  filter === f
                    ? "border-hotpink/60 bg-hotpink/15 text-pink-100"
                    : "border-white/10 bg-white/[0.03] text-haze hover:border-white/25 hover:text-white"
                }`}
              >
                {f} <span className="ml-1 tabular-nums opacity-70">{counts[f] ?? 0}</span>
              </button>
            ))}
          </div>
          <label className="relative block lg:w-72">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-haze" />
            <input
              className="input pl-9"
              placeholder="Search name, business or phone"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        </div>
      )}

      <div className="card p-5">
        {!calls ? (
          <div className="flex justify-center py-10 text-haze"><Spinner className="h-6 w-6" /></div>
        ) : calls.length === 0 ? (
          <Empty icon={<History className="h-6 w-6" />} title="No calls yet" hint="Calls show up here as soon as they're placed." />
        ) : shown.length === 0 ? (
          <Empty icon={<Search className="h-6 w-6" />} title="No matching calls" hint="Try another name, number or filter." />
        ) : (
          <>
            <p className="mb-3 text-xs text-haze">Click any call to read its full transcript.</p>
            <div className="-mx-5 overflow-x-auto">
              <table className="w-full min-w-[820px] text-left text-sm">
                <thead className="text-xs uppercase tracking-wider text-haze">
                  <tr>
                    <th className="px-5 py-2 font-semibold">When</th>
                    <th className="px-3 py-2 font-semibold">Lead</th>
                    <th className="px-3 py-2 font-semibold">Result</th>
                    <th className="px-3 py-2 font-semibold">Length</th>
                    <th className="px-3 py-2 font-semibold">Summary</th>
                    <th className="px-5 py-2 text-right font-semibold">Transcript</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((c) => (
                    <CallTableRow key={c.attempt_id} call={c} onOpen={() => router.push(`/calls/${c.attempt_id}`)} />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </>
  );
}

function CallTableRow({ call: c, onOpen }: { call: CallRow; onOpen: () => void }) {
  const lines = c.transcript_lines ?? 0;
  return (
    <tr className="table-row cursor-pointer" onClick={onOpen}>
      <td className="whitespace-nowrap px-5 py-3 text-xs text-haze">{fmtTime(c.time)}</td>
      <td className="px-3 py-3">
        <p className="font-medium">{c.name || "Unnamed"}</p>
        <p className="text-xs text-haze">
          {c.business || "—"} · {c.phone}
          {c.campaign === "Test calls" && <span className="ml-1.5 text-cyanx">test</span>}
        </p>
      </td>
      <td className="px-3 py-3"><ResultChip result={c.result} /></td>
      <td className="px-3 py-3 tabular-nums text-haze">{fmtDuration(c.talk_seconds)}</td>
      <td className="max-w-sm px-3 py-3 text-xs text-zinc-300">
        <p className="line-clamp-2">{c.summary || c.callback_time || "—"}</p>
      </td>
      <td className="whitespace-nowrap px-5 py-3 text-right">
        <Link
          href={`/calls/${c.attempt_id}`}
          onClick={(e) => e.stopPropagation()}
          className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-semibold transition ${
            lines
              ? "border-hotpink/40 bg-hotpink/10 text-pink-100 hover:bg-hotpink/20"
              : "border-white/10 text-haze hover:text-white"
          }`}
        >
          <FileText className="h-3.5 w-3.5" />
          {lines ? `View · ${lines} lines` : "View"}
        </Link>
      </td>
    </tr>
  );
}

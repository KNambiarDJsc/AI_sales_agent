"use client";

import Link from "next/link";
import { Download, History } from "lucide-react";
import { api, download, fmtDuration, fmtTime } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { Empty, ErrorNote, PageHeader, ResultChip, Spinner } from "@/components/ui";

export default function CallsPage() {
  const { data: calls, error } = usePoll(api.calls, 5000);
  return (
    <>
      <PageHeader title="Call history" kicker="Every conversation">
        <button className="btn btn-ghost" onClick={() => download("/console/export.xlsx", "leads-results.xlsx")}>
          <Download className="h-4 w-4" /> Export Excel
        </button>
      </PageHeader>
      <ErrorNote message={error} />
      <div className="card p-5">
        {!calls ? (
          <div className="flex justify-center py-10 text-haze"><Spinner className="h-6 w-6" /></div>
        ) : calls.length === 0 ? (
          <Empty icon={<History className="h-6 w-6" />} title="No calls yet" hint="Calls show up here as soon as they're placed." />
        ) : (
          <div className="-mx-5 overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="text-xs uppercase tracking-wider text-haze">
                <tr>
                  <th className="px-5 py-2 font-semibold">When</th>
                  <th className="px-3 py-2 font-semibold">Lead</th>
                  <th className="px-3 py-2 font-semibold">Result</th>
                  <th className="px-3 py-2 font-semibold">Length</th>
                  <th className="px-3 py-2 font-semibold">Summary</th>
                </tr>
              </thead>
              <tbody>
                {calls.map((c) => (
                  <tr key={c.attempt_id} className="table-row">
                    <td className="whitespace-nowrap px-5 py-3 text-xs text-haze">
                      <Link href={`/calls/${c.attempt_id}`} className="hover:text-white">{fmtTime(c.time)}</Link>
                    </td>
                    <td className="px-3 py-3">
                      <Link href={`/calls/${c.attempt_id}`} className="font-medium hover:text-hotpink">{c.name || "Unnamed"}</Link>
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
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}

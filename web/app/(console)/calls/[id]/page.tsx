"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Phone } from "lucide-react";
import { api, fmtDuration, fmtTime, type CallDetail } from "@/lib/api";
import { Bubble } from "@/components/LiveCall";
import { ErrorNote, ResultChip, Spinner } from "@/components/ui";

export default function CallDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [call, setCall] = useState<CallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.call(id).then(setCall).catch((e) => setError(e.message));
  }, [id]);

  return (
    <>
      <Link href="/calls" className="mb-6 inline-flex items-center gap-1.5 text-sm text-haze hover:text-white">
        <ArrowLeft className="h-4 w-4" /> Call history
      </Link>
      <ErrorNote message={error} />
      {!call && !error && <div className="flex justify-center py-20 text-haze"><Spinner className="h-6 w-6" /></div>}
      {call && (
        <div className="grid gap-6 lg:grid-cols-3">
          <div className="space-y-6">
            <div className="card card-glow p-6">
              <p className="font-display text-2xl font-bold">{call.name || "Unnamed lead"}</p>
              <p className="text-haze">{call.business || "—"}</p>
              <p className="mt-2 flex items-center gap-1.5 font-mono text-sm"><Phone className="h-3.5 w-3.5 text-hotpink" /> {call.phone}</p>
              <div className="mt-5"><ResultChip result={call.result} /></div>
              <dl className="mt-5 space-y-2 text-sm">
                <Row k="When" v={fmtTime(call.time)} />
                <Row k="Length" v={fmtDuration(call.talk_seconds)} />
                <Row k="Call status" v={call.status} />
                {call.callback_time && <Row k="Call back" v={call.callback_time} />}
                <Row k="List" v={call.campaign} />
              </dl>
            </div>
            {call.summary && (
              <div className="card p-6">
                <p className="label">Summary</p>
                <p className="text-sm leading-relaxed">{call.summary}</p>
              </div>
            )}
            {call.facts && (
              <div className="card p-6">
                <p className="label">What we learned</p>
                <p className="whitespace-pre-line text-sm leading-relaxed text-zinc-300">{call.facts}</p>
              </div>
            )}
          </div>
          <div className="card p-6 lg:col-span-2">
            <h2 className="mb-5 font-display text-lg font-semibold">Transcript</h2>
            {call.transcript.length === 0 ? (
              <p className="py-10 text-center text-sm text-haze">Nothing was said on this call.</p>
            ) : (
              <div className="space-y-3">
                {call.transcript.map((l, i) => <Bubble key={i} speaker={l.speaker} text={l.text} />)}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-3">
      <dt className="text-haze">{k}</dt>
      <dd className="text-right font-medium">{v || "—"}</dd>
    </div>
  );
}

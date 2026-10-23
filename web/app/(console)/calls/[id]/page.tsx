"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { ArrowLeft, Check, Copy, Download, Phone } from "lucide-react";
import { api, fmtDuration, fmtTime, type CallDetail } from "@/lib/api";
import { Bubble } from "@/components/LiveCall";
import { ErrorNote, ResultChip, Spinner } from "@/components/ui";

export default function CallDetailPage() {
  const { id } = useParams<{ id: string }>();
  const [call, setCall] = useState<CallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.call(id).then(setCall).catch((e) => setError(e.message));
  }, [id]);

  const transcriptText = () =>
    call
      ? [
          `${call.name || "Unnamed lead"} — ${call.business || ""} — ${call.phone}`,
          `${fmtTime(call.time)} · ${fmtDuration(call.talk_seconds)} · ${call.result}`,
          "",
          ...call.transcript.map((l) => `${l.speaker === "agent" ? "AI agent" : "Customer"}: ${l.text}`),
        ].join("\n")
      : "";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(transcriptText());
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Couldn't copy — select the text instead.");
    }
  };

  const save = () => {
    const url = URL.createObjectURL(new Blob([transcriptText()], { type: "text/plain;charset=utf-8" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `transcript-${(call?.name || "call").replace(/\W+/g, "-")}-${(call?.time || "").slice(0, 10)}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

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
            <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
              <div>
                <h2 className="font-display text-lg font-semibold">Transcript</h2>
                <p className="text-xs text-haze">AI agent on the left · customer on the right</p>
              </div>
              {call.transcript.length > 0 && (
                <div className="flex gap-2">
                  <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={copy}>
                    {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                    {copied ? "Copied" : "Copy"}
                  </button>
                  <button className="btn btn-ghost px-3 py-1.5 text-xs" onClick={save}>
                    <Download className="h-3.5 w-3.5" /> Download
                  </button>
                </div>
              )}
            </div>
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

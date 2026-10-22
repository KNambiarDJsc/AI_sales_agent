"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { PhoneOff, PhoneOutgoing, RotateCcw, ShieldCheck, Smartphone } from "lucide-react";
import { api, fmtDuration, type CallDetail, type LiveCall } from "@/lib/api";
import { LiveCallCard, Bubble } from "@/components/LiveCall";
import { ErrorNote, PageHeader, ResultChip, Spinner } from "@/components/ui";

export default function TestCallPage() {
  return (
    <Suspense fallback={null}>
      <TestCall />
    </Suspense>
  );
}

type Phase = "idle" | "dialing" | "live" | "done";

function TestCall() {
  const params = useSearchParams();
  const [form, setForm] = useState({
    phone: params.get("phone") ?? "",
    name: params.get("name") ?? "",
    business: params.get("business") ?? "",
    category: params.get("category") ?? "",
  });
  const [phase, setPhase] = useState<Phase>("idle");
  const [attemptId, setAttemptId] = useState<string | null>(null);
  const [live, setLive] = useState<LiveCall | null>(null);
  const [limit, setLimit] = useState(120);
  const [result, setResult] = useState<CallDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function call(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setResult(null);
    setLive(null);
    setPhase("dialing");
    try {
      const r = await api.testCall(form);
      setAttemptId(r.attempt_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not place the call");
      setPhase("idle");
    }
  }

  // Follow our call through the overview's live card; when it disappears the call ended.
  useEffect(() => {
    if (!attemptId || phase === "done") return;
    let seen = false;
    let stopped = false;
    const tick = async () => {
      try {
        const o = await api.overview();
        if (stopped) return;
        setLimit(o.max_call_seconds);
        if (o.live && o.live.attempt_id === attemptId) {
          seen = true;
          setLive(o.live);
          setPhase("live");
        } else if (seen || !o.live) {
          const detail = await api.call(attemptId).catch(() => null);
          if (stopped) return;
          if (detail && !["queued", "dialing", "ringing", "in_progress"].includes(detail.status)) {
            setResult(detail);
            setPhase("done");
            setLive(null);
          }
        }
      } catch {
        /* keep polling */
      }
    };
    tick();
    const t = setInterval(tick, 1500);
    return () => {
      stopped = true;
      clearInterval(t);
    };
  }, [attemptId, phase]);

  const busy = phase === "dialing" || phase === "live";

  return (
    <>
      <PageHeader title="Test call" kicker="Hear the agent yourself" />
      <div className="grid gap-6 lg:grid-cols-5">
        <form onSubmit={call} className="card card-glow space-y-5 p-6 lg:col-span-2">
          <div className="flex items-center gap-3">
            <div className="rounded-xl bg-brand p-2.5"><Smartphone className="h-5 w-5 text-white" /></div>
            <div>
              <p className="font-display text-lg font-semibold">Call a number now</p>
              <p className="text-xs text-haze">Any time of day · not added to your lead list</p>
            </div>
          </div>
          <div>
            <label className="label">Phone number *</label>
            <input
              className="input font-display text-lg tracking-wide"
              required
              placeholder="98765 43210"
              value={form.phone}
              onChange={(e) => setForm({ ...form, phone: e.target.value })}
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="label">Their name</label>
              <input className="input" placeholder="Naman" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>
            <div>
              <label className="label">Business</label>
              <input className="input" placeholder="Demo Business" value={form.business} onChange={(e) => setForm({ ...form, business: e.target.value })} />
            </div>
          </div>
          <div>
            <label className="label">What they sell</label>
            <input className="input" placeholder="Perfumes" value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} />
          </div>
          <ErrorNote message={error} />
          <button className="btn btn-primary w-full py-3.5 text-base" disabled={busy || !form.phone}>
            {busy ? <Spinner /> : <PhoneOutgoing className="h-5 w-5" />}
            {phase === "dialing" ? "Dialing…" : phase === "live" ? "On the call" : "Call me now"}
          </button>
          <p className="flex items-start gap-2 text-xs text-haze">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-cyanx" />
            Calls end automatically at 2 minutes. Numbers on the do-not-call list are never dialled. One test call at a time.
          </p>
        </form>

        <div className="lg:col-span-3">
          {live ? (
            <LiveCallCard live={live} limit={limit} />
          ) : phase === "dialing" ? (
            <div className="card flex flex-col items-center gap-4 p-12 text-center">
              <div className="flex h-16 w-16 items-center justify-center rounded-full bg-volt text-ink-950 animate-pulse-ring">
                <PhoneOutgoing className="h-7 w-7" />
              </div>
              <p className="font-display text-xl font-semibold">Ringing {form.phone}…</p>
              <p className="text-sm text-haze">Pick up and talk. The conversation appears here as it happens.</p>
              {attemptId && (
                <button className="btn btn-danger" onClick={() => api.hangup(attemptId).catch(() => {})}>
                  <PhoneOff className="h-4 w-4" /> Cancel call
                </button>
              )}
            </div>
          ) : result ? (
            <div className="card p-6">
              <div className="mb-4 flex flex-wrap items-center gap-3">
                <p className="mr-auto font-display text-xl font-semibold">Call finished</p>
                <ResultChip result={result.result} />
                <span className="text-sm text-haze">{fmtDuration(result.talk_seconds)}</span>
              </div>
              {result.summary && <p className="mb-4 rounded-xl bg-white/[0.04] p-3 text-sm">{result.summary}</p>}
              <div className="max-h-96 space-y-3 overflow-y-auto">
                {result.transcript.map((l, i) => <Bubble key={i} speaker={l.speaker} text={l.text} />)}
              </div>
              <div className="mt-5 flex gap-2">
                <button className="btn btn-ghost" onClick={() => { setPhase("idle"); setResult(null); setAttemptId(null); }}>
                  <RotateCcw className="h-4 w-4" /> New test
                </button>
                <Link href={`/calls/${result.attempt_id}`} className="btn btn-ghost">Full details</Link>
              </div>
            </div>
          ) : (
            <div className="card flex h-full min-h-72 flex-col items-center justify-center gap-2 p-10 text-center">
              <p className="font-display text-xl font-semibold">Your phone rings in a few seconds</p>
              <p className="max-w-sm text-sm text-haze">
                Enter your own number, hit call, and talk to the agent like a lead would. You can reply in English, Hindi or Hinglish.
              </p>
            </div>
          )}
        </div>
      </div>
    </>
  );
}

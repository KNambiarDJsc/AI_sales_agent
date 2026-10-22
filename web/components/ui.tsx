"use client";

import { Loader2 } from "lucide-react";
import type { Result } from "@/lib/api";

const RESULT_STYLE: Record<string, string> = {
  Interested: "bg-volt/15 text-volt border-volt/40",
  "Callback requested": "bg-amber-400/15 text-amber-300 border-amber-400/40",
  "Not interested": "bg-rose-400/12 text-rose-300 border-rose-400/35",
  "Do not call": "bg-red-500/15 text-red-300 border-red-500/45",
  Unclear: "bg-cyanx/12 text-cyanx border-cyanx/35",
  "Calling…": "bg-hotpink/15 text-pink-200 border-hotpink/50",
  "Not called yet": "bg-white/5 text-haze border-white/10",
};

export function ResultChip({ result }: { result: Result | string }) {
  const style = RESULT_STYLE[result] ?? "bg-white/5 text-zinc-300 border-white/12";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-semibold ${style}`}>
      {result === "Calling…" && <span className="h-1.5 w-1.5 rounded-full bg-hotpink animate-pulse" />}
      {result}
    </span>
  );
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return <Loader2 className={`${className} animate-spin`} />;
}

export function PageHeader({ title, kicker, children }: { title: string; kicker?: string; children?: React.ReactNode }) {
  return (
    <div className="mb-8 flex flex-wrap items-end justify-between gap-4">
      <div>
        {kicker && <p className="mb-1 text-xs font-semibold uppercase tracking-[0.2em] text-hotpink">{kicker}</p>}
        <h1 className="font-display text-3xl font-bold tracking-tight sm:text-4xl">{title}</h1>
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  );
}

export function ErrorNote({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">{message}</div>
  );
}

export function EqBars({ active = true }: { active?: boolean }) {
  return (
    <span className="inline-flex h-4 items-end gap-[3px]">
      {[0, 0.15, 0.3, 0.45].map((d) => (
        <span
          key={d}
          className={`w-[3px] origin-bottom rounded-full bg-volt ${active ? "animate-bar" : "scale-y-30"}`}
          style={{ height: "100%", animationDelay: `${d}s` }}
        />
      ))}
    </span>
  );
}

export function Empty({ icon, title, hint }: { icon: React.ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-14 text-center">
      <div className="mb-2 rounded-2xl bg-white/5 p-4 text-haze">{icon}</div>
      <p className="font-display text-lg font-semibold">{title}</p>
      {hint && <p className="max-w-sm text-sm text-haze">{hint}</p>}
    </div>
  );
}

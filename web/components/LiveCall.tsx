"use client";

import { useEffect, useRef } from "react";
import { Bot, PhoneCall, User } from "lucide-react";
import type { LiveCall as Live } from "@/lib/api";
import { EqBars } from "@/components/ui";

const STATE_LABEL: Record<string, string> = {
  INTRO: "Greeting",
  PITCH: "Pitching",
  CLARIFY: "Clarifying",
  PERMISSION: "Asking for a minute",
  DISCOVERY: "Learning about the business",
  QUALIFICATION: "Qualifying",
  OBJECTION_HANDLING: "Handling an objection",
  INTERESTED: "Interested!",
  CALLBACK: "Capturing a follow-up",
  NOT_INTERESTED: "Not interested",
  DO_NOT_CALL: "Do not call",
  UNCERTAIN: "Handing to a human",
  WRONG_NUMBER: "Wrong number",
  END: "Wrapping up",
};

function mmss(s: number) {
  return `${Math.floor(s / 60)}:${(s % 60).toString().padStart(2, "0")}`;
}

export function LiveCallCard({ live }: { live: Live }) {
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [live.lines.length]);

  const talking = live.status === "talking";
  return (
    <div className="card card-glow overflow-hidden">
      <div className="flex flex-wrap items-center gap-4 border-b border-white/5 p-5">
        <div className="relative flex h-12 w-12 items-center justify-center rounded-full bg-volt text-ink-950 animate-pulse-ring">
          <PhoneCall className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="rounded-md bg-volt px-1.5 py-0.5 text-[10px] font-bold tracking-widest text-ink-950">LIVE</span>
            {live.test && (
              <span className="rounded-md bg-cyanx/15 px-1.5 py-0.5 text-[10px] font-bold tracking-widest text-cyanx">TEST</span>
            )}
            <EqBars active={talking} />
          </div>
          <p className="mt-1 truncate font-display text-xl font-semibold">
            {live.name || "Unnamed lead"} <span className="text-haze">· {live.business || "—"}</span>
          </p>
          <p className="text-sm text-haze">{live.phone}</p>
        </div>
        <div className="text-right">
          <p className="font-display text-3xl font-bold tabular-nums">{mmss(live.seconds)}</p>
          <p className="text-xs font-medium text-hotpink">
            {talking ? STATE_LABEL[live.state ?? ""] ?? live.state ?? "Talking" : "Ringing…"}
          </p>
        </div>
      </div>
      <div ref={scroller} className="max-h-80 space-y-3 overflow-y-auto p-5">
        {live.lines.length === 0 && (
          <p className="py-6 text-center text-sm text-haze">
            {talking ? "Listening…" : "Waiting for them to pick up…"}
          </p>
        )}
        {live.lines.map((line, i) => (
          <Bubble key={i} speaker={line.speaker} text={line.text} />
        ))}
      </div>
    </div>
  );
}

export function Bubble({ speaker, text }: { speaker: "agent" | "customer"; text: string }) {
  const agent = speaker === "agent";
  return (
    <div className={`flex gap-2.5 ${agent ? "" : "flex-row-reverse"}`}>
      <div
        className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${
          agent ? "bg-brand text-white" : "bg-cyanx/20 text-cyanx"
        }`}
      >
        {agent ? <Bot className="h-3.5 w-3.5" /> : <User className="h-3.5 w-3.5" />}
      </div>
      <div
        className={`max-w-[80%] rounded-2xl px-3.5 py-2 text-sm leading-relaxed ${
          agent ? "rounded-tl-sm bg-white/[0.06]" : "rounded-tr-sm bg-cyanx/10 text-cyan-50"
        }`}
      >
        {text}
      </div>
    </div>
  );
}

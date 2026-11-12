"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  CheckCircle2,
  FileText,
  FileUp,
  Mic,
  PencilLine,
  PhoneOutgoing,
  Plus,
  RotateCcw,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";
import { api, type LanguageStyle, type ScriptContent, type ScriptDraft, type ScriptList, type ScriptSummary } from "@/lib/api";
import { openDemo } from "@/components/ConsoleShell";
import { ErrorNote, PageHeader, Spinner } from "@/components/ui";

const STYLES: { key: LanguageStyle; label: string; hint: string }[] = [
  { key: "hinglish", label: "Hinglish", hint: "Hindi with everyday English words — “क्या आप interested रहेंगे?”" },
  { key: "hindi", label: "Hindi", hint: "Simple spoken Hindi" },
  { key: "english", label: "English", hint: "Indian English" },
];
const STYLE_LABEL: Record<string, string> = { hinglish: "Hinglish", hindi: "Hindi", english: "English" };

const BLANK: ScriptContent = {
  name: "",
  description: "",
  language_style: "hinglish",
  agent_name: "",
  company_name: "",
  opening_lines: [""],
  script_text: "",
  knowledge: {},
  objection_playbook: {},
  product_info: "",
  target_customer: "",
  source: "written",
  source_filename: "",
};

type Editing = { id: string | null; content: ScriptContent; note?: string; original?: string; builtin?: boolean };

export default function ScriptsPage() {
  const [list, setList] = useState<ScriptList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [reading, setReading] = useState<string | null>(null);
  const [pasting, setPasting] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(() => api.scripts().then(setList).catch((e) => setError(e.message)), []);
  useEffect(() => {
    load();
  }, [load]);

  function openDraft(result: ScriptDraft, from: string) {
    setEditing({
      id: null,
      content: { ...result.draft, opening_lines: result.draft.opening_lines.length ? result.draft.opening_lines : [""] },
      original: result.original_text,
      note: result.organised
        ? `Read ${from}. The AI sorted it into the parts below without adding anything — check it, change what you like, then save.`
        : `Read ${from}. It could not be sorted automatically, so the whole text is in “What to say” as it is. Add a first line and save.`,
    });
  }

  async function readFile(file: File) {
    setError(null);
    setReading(file.name);
    try {
      openDraft(await api.readScriptFile(file), `“${file.name}”`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the file");
    } finally {
      setReading(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function edit(script: ScriptSummary) {
    setError(null);
    setBusyId(script.id);
    try {
      const full = await api.script(script.id);
      setEditing({ id: full.id, builtin: full.builtin, content: { ...full, opening_lines: full.opening_lines.length ? full.opening_lines : [""] } });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not open the script");
    } finally {
      setBusyId(null);
    }
  }

  async function act(id: string, action: () => Promise<unknown>) {
    setError(null);
    setBusyId(id);
    try {
      await action();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "That didn't work");
    } finally {
      setBusyId(null);
    }
  }

  if (editing) {
    return (
      <Editor
        editing={editing}
        fields={list?.fields ?? []}
        maxChars={list?.max_chars ?? 12000}
        onClose={() => setEditing(null)}
        onSaved={async () => {
          setEditing(null);
          await load();
        }}
      />
    );
  }

  const active = list?.scripts.find((s) => s.active);
  return (
    <>
      <PageHeader title="Scripts" kicker="What the agent says">
        <button className="btn btn-ghost" onClick={() => setEditing({ id: null, content: BLANK })}>
          <Plus className="h-4 w-4" /> Write one
        </button>
        <button className="btn btn-ghost" onClick={() => setPasting(true)}>
          <PencilLine className="h-4 w-4" /> Paste text
        </button>
        <button className="btn btn-primary" onClick={() => fileInput.current?.click()} disabled={!!reading}>
          {reading ? <Spinner /> : <FileUp className="h-4 w-4" />} Upload a script
        </button>
        <input
          ref={fileInput}
          type="file"
          className="hidden"
          accept=".txt,.md,.docx,.pdf,.rtf,text/plain,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          onChange={(e) => e.target.files?.[0] && readFile(e.target.files[0])}
        />
      </PageHeader>

      <div className="mb-6 space-y-3">
        <ErrorNote message={error} />
        {reading && (
          <div className="flex items-center gap-3 rounded-xl border border-ultra/30 bg-ultra/5 px-4 py-3 text-sm">
            <Spinner /> Reading “{reading}” and sorting it into a call script. This takes 10–30 seconds…
          </div>
        )}
      </div>

      {pasting && <PasteBox onClose={() => setPasting(false)} onRead={(r) => { setPasting(false); openDraft(r, "your text"); }} />}

      {!list ? (
        <div className="flex justify-center py-20 text-haze"><Spinner className="h-6 w-6" /></div>
      ) : (
        <>
          {active && (
            <div className="card card-glow mb-6 p-6">
              <p className="mb-1 flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-volt">
                <CheckCircle2 className="h-4 w-4" /> Calls are using this script
              </p>
              <p className="font-display text-2xl font-bold">{active.name}</p>
              <p className="mt-1 text-sm text-haze">{active.description}</p>
              {active.opening_line && (
                <p className="mt-4 rounded-xl bg-ink-950 p-4 text-sm leading-relaxed">
                  <span className="mb-1 block text-[11px] font-semibold uppercase tracking-wider text-haze">First line</span>
                  {active.opening_line}
                </p>
              )}
            </div>
          )}

          <div className="grid gap-4 md:grid-cols-2">
            {list.scripts.map((s) => (
              <div key={s.id} className={`card flex flex-col p-5 ${s.active ? "border-volt/50" : ""}`}>
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="rounded-full bg-ultra/10 px-2.5 py-0.5 text-xs font-semibold text-ultra">{STYLE_LABEL[s.language_style]}</span>
                  <span className="rounded-full bg-black/5 px-2.5 py-0.5 text-xs font-semibold text-haze">
                    {s.builtin ? (s.edited ? "Built-in · edited" : "Built-in") : s.source === "uploaded" ? "Uploaded" : "Written here"}
                  </span>
                  {s.active && <span className="rounded-full bg-volt/15 px-2.5 py-0.5 text-xs font-semibold text-volt">In use</span>}
                </div>
                <p className="font-display text-lg font-semibold">{s.name}</p>
                <p className="mt-1 line-clamp-2 text-sm text-haze">{s.description || s.source_filename || "—"}</p>
                <p className="mt-3 line-clamp-2 flex-1 text-sm text-zinc-700">{s.opening_line}</p>
                <div className="mt-4 flex flex-wrap gap-2">
                  {!s.active && (
                    <button className="btn btn-volt" disabled={busyId === s.id} onClick={() => act(s.id, () => api.activateScript(s.id))}>
                      {busyId === s.id ? <Spinner /> : <CheckCircle2 className="h-4 w-4" />} Use for calls
                    </button>
                  )}
                  <button className="btn btn-ghost" disabled={busyId === s.id} onClick={() => edit(s)}>
                    <PencilLine className="h-4 w-4" /> {s.builtin ? "View / edit" : "Edit"}
                  </button>
                  <button className="btn btn-ghost" title="Talk to the agent in your browser using this script — no phone call" onClick={() => openDemo({ script: s.id })}>
                    <Mic className="h-4 w-4" /> Try
                  </button>
                  <Link className="btn btn-ghost" href={`/test-call?script=${encodeURIComponent(s.id)}`} title="Call your own phone with this script">
                    <PhoneOutgoing className="h-4 w-4" />
                  </Link>
                  {(!s.builtin || s.edited) && (
                    <button
                      className="btn btn-ghost text-rose-700"
                      disabled={busyId === s.id}
                      title={s.builtin ? "Undo your edits" : "Delete"}
                      onClick={() => {
                        if (window.confirm(s.builtin ? `Undo your edits to “${s.name}”?` : `Delete “${s.name}”? This can't be undone.`)) {
                          act(s.id, () => api.deleteScript(s.id));
                        }
                      }}
                    >
                      {s.builtin ? <RotateCcw className="h-4 w-4" /> : <Trash2 className="h-4 w-4" />}
                    </button>
                  )}
                </div>
              </div>
            ))}
          </div>

          <div className="card mt-6 p-5 text-sm text-haze">
            <p className="mb-2 flex items-center gap-2 font-display text-base font-semibold text-ink">
              <Sparkles className="h-4 w-4 text-ultra" /> How your own script works
            </p>
            <ol className="list-decimal space-y-1 pl-5">
              <li>Upload the script your callers use — a Word file, PDF or text file — in Hindi, Hinglish or English.</li>
              <li>The console reads it and shows you exactly what the agent will say. Change anything you like.</li>
              <li>Press <span className="font-semibold text-ink">Try</span> to talk to it in your browser, then <span className="font-semibold text-ink">Use for calls</span>.</li>
            </ol>
            <p className="mt-2">
              The agent follows your script but talks like a person: it answers questions from your script&apos;s facts, handles a “no” the way your
              script says, and books a call-back time for interested leads. It never invents prices or promises that aren&apos;t in your script.
            </p>
          </div>
        </>
      )}
    </>
  );
}

function PasteBox({ onClose, onRead }: { onClose: () => void; onRead: (r: ScriptDraft) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <div className="card mb-6 space-y-3 p-5">
      <div className="flex items-center justify-between">
        <p className="font-display text-lg font-semibold">Paste your script</p>
        <button onClick={onClose} aria-label="Close" className="text-haze hover:text-ink"><X className="h-5 w-5" /></button>
      </div>
      <textarea className="input min-h-48" placeholder="Paste the script your callers use…" value={text} onChange={(e) => setText(e.target.value)} />
      <ErrorNote message={error} />
      <button
        className="btn btn-primary"
        disabled={busy || text.trim().length < 20}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            onRead(await api.readScriptText(text));
          } catch (e) {
            setError(e instanceof Error ? e.message : "Could not read the text");
            setBusy(false);
          }
        }}
      >
        {busy ? <Spinner /> : <Sparkles className="h-4 w-4" />} {busy ? "Reading… (10–30 s)" : "Read it"}
      </button>
    </div>
  );
}

function Editor({
  editing,
  fields,
  maxChars,
  onClose,
  onSaved,
}: {
  editing: Editing;
  fields: string[];
  maxChars: number;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [c, setC] = useState<ScriptContent>(editing.content);
  const [busy, setBusy] = useState<"save" | "use" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sample, setSample] = useState({ business_name: "Sharma Electronics", contact_name: "", brand: "", email: "" });
  const [preview, setPreview] = useState<{ line: string; from_script: boolean } | null>(null);
  const [showOriginal, setShowOriginal] = useState(false);
  const set = <K extends keyof ScriptContent>(key: K, value: ScriptContent[K]) => setC((old) => ({ ...old, [key]: value }));

  // The exact first line a lead would hear, as the client types.
  useEffect(() => {
    const t = setTimeout(() => {
      api
        .previewOpening({
          opening_lines: c.opening_lines.filter((l) => l.trim()),
          agent_name: c.agent_name,
          company_name: c.company_name,
          language_style: c.language_style,
          lead: sample,
        })
        .then((p) => {
          setPreview(p);
          setError((e) => (e && e.startsWith("The first line") ? null : e));
        })
        .catch((e) => {
          setPreview(null);
          if (e instanceof Error && e.message.startsWith("The first line")) setError(e.message);
        });
    }, 400);
    return () => clearTimeout(t);
  }, [c.opening_lines, c.agent_name, c.company_name, c.language_style, sample]);

  async function save(use: boolean) {
    setError(null);
    setBusy(use ? "use" : "save");
    try {
      const body = { ...c, opening_lines: c.opening_lines.map((l) => l.trim()).filter(Boolean) };
      const { id } = editing.id ? await api.updateScript(editing.id, body) : await api.createScript(body);
      if (use) await api.activateScript(id);
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save");
      setBusy(null);
    }
  }

  const tooLong = c.script_text.length > maxChars;
  const canSave = c.name.trim().length > 0 && c.script_text.trim().length >= 20 && !tooLong;

  return (
    <>
      <button onClick={onClose} className="mb-6 inline-flex items-center gap-1.5 text-sm text-haze hover:text-ink">
        <ArrowLeft className="h-4 w-4" /> All scripts
      </button>
      <PageHeader title={editing.id ? "Edit script" : "New script"} kicker="What the agent says">
        <button className="btn btn-ghost" disabled={!canSave || !!busy} onClick={() => save(false)}>
          {busy === "save" && <Spinner />} Save
        </button>
        <button className="btn btn-volt" disabled={!canSave || !!busy} onClick={() => save(true)}>
          {busy === "use" ? <Spinner /> : <CheckCircle2 className="h-4 w-4" />} Save &amp; use for calls
        </button>
      </PageHeader>

      <div className="mb-6 space-y-3">
        {editing.note && (
          <div className="flex items-start gap-2 rounded-xl border border-ultra/30 bg-ultra/5 px-4 py-3 text-sm">
            <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-ultra" /> <span>{editing.note}</span>
          </div>
        )}
        {editing.builtin && (
          <div className="rounded-xl border border-ink-700 bg-ink-950 px-4 py-3 text-sm text-haze">
            This is a built-in script. Your changes are saved as your own version; you can undo them any time from the scripts list.
          </div>
        )}
        <ErrorNote message={error} />
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-2">
          <div className="card space-y-4 p-6">
            <div>
              <label className="label" htmlFor="s-name">Script name *</label>
              <input id="s-name" className="input" maxLength={80} placeholder="Amazon onboarding — fresh sellers" value={c.name} onChange={(e) => set("name", e.target.value)} />
            </div>
            <div>
              <span className="label">Language the agent speaks</span>
              <div className="grid gap-2 sm:grid-cols-3">
                {STYLES.map((s) => (
                  <button
                    key={s.key}
                    type="button"
                    onClick={() => set("language_style", s.key)}
                    className={`rounded-xl border p-3 text-left text-sm transition ${
                      c.language_style === s.key ? "border-ultra bg-ultra/5" : "border-ink-600 hover:border-ultra/50"
                    }`}
                  >
                    <span className="block font-semibold">{s.label}</span>
                    <span className="text-xs text-haze">{s.hint}</span>
                  </button>
                ))}
              </div>
              <p className="mt-2 text-xs text-haze">If the customer answers in another of these languages, the agent switches to it by itself.</p>
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <label className="label" htmlFor="s-agent">Agent&apos;s name</label>
                <input id="s-agent" className="input" maxLength={40} placeholder="श्रुति" value={c.agent_name} onChange={(e) => set("agent_name", e.target.value)} />
              </div>
              <div>
                <label className="label" htmlFor="s-company">Calling from (company)</label>
                <input id="s-company" className="input" maxLength={80} placeholder="Your company name" value={c.company_name} onChange={(e) => set("company_name", e.target.value)} />
              </div>
            </div>
          </div>

          <div className="card space-y-3 p-6">
            <div>
              <p className="font-display text-lg font-semibold">First line of the call</p>
              <p className="text-sm text-haze">
                Said the moment the lead picks up. Add a second line as a backup for leads whose details are missing — the first line that fits each lead is used.
              </p>
            </div>
            {c.opening_lines.map((line, i) => (
              <div key={i} className="flex gap-2">
                <textarea
                  className="input min-h-20"
                  placeholder="नमस्कार, मैं {agent_name} बात कर रही हूँ {company_name} से। क्या मेरी बात {business_name} से हो रही है?"
                  value={line}
                  onChange={(e) => set("opening_lines", c.opening_lines.map((l, j) => (j === i ? e.target.value : l)))}
                />
                {c.opening_lines.length > 1 && (
                  <button aria-label="Remove line" className="text-haze hover:text-rose-700" onClick={() => set("opening_lines", c.opening_lines.filter((_, j) => j !== i))}>
                    <X className="h-4 w-4" />
                  </button>
                )}
              </div>
            ))}
            <div className="flex flex-wrap items-center gap-2 text-xs text-haze">
              Blanks you can use:
              {fields.map((f) => (
                <button
                  key={f}
                  type="button"
                  className="rounded-md bg-black/5 px-2 py-1 font-mono text-[11px] text-ink hover:bg-ultra/10"
                  title="Add to the last line"
                  onClick={() => set("opening_lines", c.opening_lines.map((l, j) => (j === c.opening_lines.length - 1 ? `${l}{${f}}` : l)))}
                >
                  {`{${f}}`}
                </button>
              ))}
              {c.opening_lines.length < 4 && (
                <button type="button" className="ml-auto font-semibold text-ultra hover:underline" onClick={() => set("opening_lines", [...c.opening_lines, ""])}>
                  + Add a backup line
                </button>
              )}
            </div>
          </div>

          <div className="card space-y-3 p-6">
            <div className="flex items-end justify-between gap-3">
              <div>
                <p className="font-display text-lg font-semibold">What to say *</p>
                <p className="text-sm text-haze">The pitch, what to do on yes and on no, and how to close. Write it the way you&apos;d brief a new caller.</p>
              </div>
              <span className={`shrink-0 text-xs tabular-nums ${tooLong ? "font-semibold text-rose-700" : "text-haze"}`}>
                {c.script_text.length.toLocaleString()} / {maxChars.toLocaleString()}
              </span>
            </div>
            <textarea className="input min-h-96 font-[450]" value={c.script_text} onChange={(e) => set("script_text", e.target.value)} />
            {tooLong && <p className="text-sm text-rose-700">Too long. Keep only what the agent should say on the call.</p>}
            {editing.original && (
              <div>
                <button className="text-sm font-semibold text-ultra hover:underline" onClick={() => setShowOriginal((v) => !v)}>
                  {showOriginal ? "Hide" : "Show"} the text exactly as it was in your file
                </button>
                {showOriginal && <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap rounded-xl bg-ink-950 p-4 text-xs leading-relaxed">{editing.original}</pre>}
              </div>
            )}
          </div>

          <Pairs
            title="Facts the agent may state"
            hint="Prices, timelines, what is free, what is needed. The agent answers questions only from these and your script — anything else it leaves to your team."
            keyLabel="Topic"
            valueLabel="Fact"
            value={c.knowledge}
            onChange={(v) => set("knowledge", v)}
          />
          <Pairs
            title="How to answer objections"
            hint="For each reason a lead says no, how the agent should respond."
            keyLabel="Concern"
            valueLabel="How to answer"
            value={c.objection_playbook}
            onChange={(v) => set("objection_playbook", v)}
          />
        </div>

        <div className="space-y-6">
          <div className="card space-y-3 p-6 lg:sticky lg:top-6">
            <p className="flex items-center gap-2 font-display text-lg font-semibold"><FileText className="h-4 w-4 text-ultra" /> What a lead hears first</p>
            <p className="min-h-16 rounded-xl bg-ink-950 p-4 text-sm leading-relaxed">{preview?.line || "…"}</p>
            {preview && !preview.from_script && (
              <p className="text-xs text-amber-700">
                None of your first lines fits this lead (a blank it needs is empty), so the standard greeting is used. Add a backup line without that blank.
              </p>
            )}
            <p className="text-xs font-semibold uppercase tracking-wider text-haze">Try with a sample lead</p>
            {(["business_name", "contact_name", "brand", "email"] as const).map((f) => (
              <input key={f} className="input py-2 text-sm" placeholder={f.replace("_", " ")} value={sample[f]} onChange={(e) => setSample({ ...sample, [f]: e.target.value })} />
            ))}
            <p className="text-xs text-haze">Leads get these from your Excel columns: Business, Name, Brand, Email.</p>
          </div>
        </div>
      </div>
    </>
  );
}

function Pairs({
  title,
  hint,
  keyLabel,
  valueLabel,
  value,
  onChange,
}: {
  title: string;
  hint: string;
  keyLabel: string;
  valueLabel: string;
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
}) {
  // Rows are edited as a list so a half-typed topic doesn't collide with another.
  const [rows, setRows] = useState<[string, string][]>(() => Object.entries(value));
  const update = (next: [string, string][]) => {
    setRows(next);
    onChange(Object.fromEntries(next.filter(([k, v]) => k.trim() && v.trim()).map(([k, v]) => [k.trim(), v.trim()])));
  };
  return (
    <div className="card space-y-3 p-6">
      <div>
        <p className="font-display text-lg font-semibold">{title}</p>
        <p className="text-sm text-haze">{hint}</p>
      </div>
      {rows.map(([k, v], i) => (
        <div key={i} className="grid gap-2 sm:grid-cols-[11rem_1fr_auto]">
          <input className="input py-2 text-sm" aria-label={keyLabel} placeholder={keyLabel} value={k} onChange={(e) => update(rows.map((r, j) => (j === i ? [e.target.value, r[1]] : r)))} />
          <textarea className="input min-h-10 py-2 text-sm" rows={2} aria-label={valueLabel} placeholder={valueLabel} value={v} onChange={(e) => update(rows.map((r, j) => (j === i ? [r[0], e.target.value] : r)))} />
          <button aria-label="Remove" className="text-haze hover:text-rose-700" onClick={() => update(rows.filter((_, j) => j !== i))}>
            <X className="h-4 w-4" />
          </button>
        </div>
      ))}
      <button className="text-sm font-semibold text-ultra hover:underline" onClick={() => update([...rows, ["", ""]])}>
        + Add
      </button>
    </div>
  );
}

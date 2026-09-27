# Status

Last updated: 2026-09-27 (latency/realtime pass). Read this before adding anything — it
tracks what's real vs. placeholder, and what happens when client materials (code,
prompts, scripts, credentials, models) arrive.

## What exists and is real code (not stubs)

- **Config system** (`config/settings.py`, `config/prompts/`, `config/scripts/`,
  `config/qualification/`): env-driven settings, versioned script/prompt/qualification
  YAML. No business logic in Python — changing the script or thresholds means editing
  YAML, not code.
- **Database** (`database/`): full SQLAlchemy 2.0 models for all 17 core tables
  (Section 15), repositories with tenant/campaign scoping and `FOR UPDATE SKIP LOCKED`
  leasing, and a hand-written Alembic migration (`0001_initial_schema.py`) matching the
  models exactly. **Not yet run against a live Postgres in this environment** — Docker
  Desktop wasn't running here. Before trusting the migration, run: `docker compose up
  -d db && alembic upgrade head` and confirm it applies cleanly.
- **Telephony** (`telephony/`): `TelephonyProvider` interface; `TwilioProvider` fully
  implemented against Twilio's documented REST + Media Streams APIs; `ExotelProvider`
  implemented against Exotel's documented Connect API, with the streaming/App-Bazaar
  wiring flagged as needing account-specific confirmation (see its module docstring);
  `FreejunProvider` is an intentional stub — no verified public API docs exist for
  Freejun, so every method raises `NotImplementedError` with exactly what's needed to
  implement it for real.
- **Speech** (`speech/`): `STTProvider`/`TTSProvider` interfaces; OpenAI adapters using
  the documented REST `audio.transcriptions` and streaming `audio.speech` endpoints.
  STT is per-utterance (buffered until endpointing signals turn-end), not token-level
  streaming partials — see `speech/stt/openai.py`'s docstring for why, and how to swap
  in the Realtime API later without touching callers.
- **LLM** (`llm/`): `LLMProvider` interface + OpenAI adapter using structured outputs
  (`response_format: json_schema, strict: true`) so the model's output is
  schema-constrained at the API level, not just hoped-for.
- **Orchestration** (`orchestrator/`): config-driven state machine, prompt assembly,
  and — critically — `validator.py`, which is the actual security/reliability boundary
  (Section 22): every LLM response is parsed, schema-validated, and checked against the
  active script's allowed transitions before anything acts on it. Malformed or
  state-invalid output always falls back to the script's own fallback line and current
  state; a disallowed *tool* call, if the state/speech are otherwise fine, is just
  dropped rather than discarding a good response (see "Latency & realtime" below).
  `orchestrator/engine.py` wires prompts → LLM → validator → tools → state transition
  into one `run_turn()` call, and never leaves the LLM call unbounded (Section 23's
  "explicit deadlines" — see below).
- **Tools** (`tools/`): typed internal registry (no MCP), one module per tool
  (`mark_dnc`, `schedule_callback`, `create_qualification`, `update_lead`, `end_call`,
  `export_lead`), each with a Pydantic input schema, state-authorization double-check,
  and an explicit allow-list of mutable fields. `create_qualification` does NOT let the
  LLM set `qualified`/`outcome` directly — see qualification below.
- **Qualification** (`qualification/`): config-driven scoring (`rules.py` +
  `scoring.py`) that checks DNC first (always overrides), then disqualification
  conditions, then the configured `all_of` fact/confidence bar, routing anything
  under-confidence to `uncertain` rather than guessing.
- **Voice/media** (`voice/`): mu-law↔PCM16 + resampling (`audio/processing.py`, using
  real resampling via `audioop.ratecv`, never sample duplication), fixed-size outbound
  frame re-chunking (`FrameChunker`), WebRTC VAD (`vad/vad.py`), a config-driven
  endpointing state machine separate from VAD (`turn_taking/turn_taking.py`), and
  `VoiceSession` (`session/session.py`) which wires all of it together plus barge-in
  (Section 18: cancel TTS, clear provider audio, cancel the speaking task on new
  customer speech) and pipelined, sentence-by-sentence TTS playback. See "Latency &
  realtime" below for the reasoning.
- **API** (`apps/api/`): FastAPI app with campaign CRUD + lead CSV/XLSX import (column
  mapping is guessed and reported back, never silently trusted for an unrecognized
  file), lead endpoints, a Twilio/Exotel-shaped webhook handler with signature
  verification, and the realtime media WebSocket endpoint that instantiates a
  `VoiceSession` per call.
- **Worker** (`workers/`, `services/call_worker/`): polling dispatch loop respecting
  call window + concurrency limits, idempotent call-attempt creation
  (`campaign_id+lead_id+attempt_number` unique constraint), a retry sweep, and CSV
  export.
- **Observability** (`observability/`): OpenTelemetry tracing scaffold + structured
  logging with basic PII-key redaction.
- **Tests** (`tests/unit/`): 49 real, running unit tests (no external services needed)
  covering lead import/normalization, the state machine's transition rules, the
  LLM-output validator's fallback behavior (including the granular tool-vs-state
  distinction), qualification scoring, turn-taking/endpointing timing, the speculative
  streaming extractor's safety gating, the engine's speech-callback contract (never
  double-speaks, even across simulated mid-stream failures), the outbound frame
  chunker, the sentence splitter, and an integration-style proof that TTS sentence
  pipelining is genuinely concurrent (not just sequential-looking) plus that barge-in
  cancellation leaves no leaked tasks or stray audio. Run: `pytest tests/unit`.

## Latency & realtime (this pass)

The previous version of this codebase was architecturally sound but had an
unnecessarily long turn-around per conversational turn: full non-streaming LLM call →
DB round trip for any tool call → THEN start speaking. Fixed, in order of impact:

1. **Tool execution no longer blocks speech.** `orchestrator/engine.py:run_turn` takes
   an `on_speech_ready` callback and calls it as soon as the agent's line is known;
   `voice/session/session.py` starts the TTS/telephony task from that callback and does
   NOT wait for `run_turn` to finish (which still runs the tool call and state
   transition, just no longer on the path to the customer hearing a response). A
   `mark_dnc`/`end_call` tool is still always awaited before the caller decides to hang
   up — ordering is safe, not just faster.
2. **A disallowed tool call no longer discards a good response.** `orchestrator/
   validator.py` used to replace the *entire* turn with a generic fallback line if
   *either* the state *or* the tool was invalid. Now: an invalid state is still a full
   fallback (the spoken text is usually written assuming that transition happened, so
   keeping it while refusing the transition would be incoherent); an invalid tool with
   a fine state/speech just gets dropped, keeping the natural response.
3. **Optional speculative TTS start** (`settings.enable_speculative_tts`, default
   **off**): streams the LLM's structured-output JSON (`llm/openai.py:propose_stream`)
   and starts speaking as soon as the `speech` field has fully arrived — before
   `tool_call`/`extracted_facts` finish streaming — instead of waiting for the whole
   object. Safety: gated on the `state` field having already passed the same
   `is_transition_allowed` check the non-streaming path uses
   (`orchestrator/streaming.py:SpeculativeTurnExtractor`); if state is invalid, nothing
   is spoken early. `orchestrator/engine.py` guarantees the speech callback fires
   *exactly once* per turn even if the stream fails partway through after speech was
   already spoken (tested in `tests/unit/test_engine_streaming.py`, including the
   "already spoke, stream then dies" edge case). **Default is off** because this
   environment has no live OpenAI key to confirm streaming actually arrives
   incrementally alongside strict `json_schema` structured outputs — flip it on, watch
   for `speculative_tts_mismatch`/`speculative_tts_error` in logs, and confirm it
   measurably reduces time-to-first-audio before trusting it in production.
4. **TTS sentence pipelining** (`settings.enable_tts_sentence_pipelining`, default on):
   `voice/session/session.py:_speak` splits multi-sentence responses
   (`voice/session/sentence_split.py`) and synthesizes every sentence concurrently
   (each gets its own OpenAI TTS request immediately, not after the previous sentence
   finishes), draining them to the telephony leg in order. A single-sentence response
   is unaffected. Fixed a real bug this depended on:
   `speech/tts/openai.py`'s `OpenAITTSProvider` used one shared, clear-on-start
   cancellation flag — fine with one utterance in flight at a time, broken the moment
   two can overlap (a fresh sentence's stream would un-cancel an older one mid-teardown).
   Now every `synthesize_stream()` call gets its own token; `cancel()` still stops all
   of them at once for barge-in.
5. **Fixed-size outbound audio frames** (`voice/audio/processing.py:FrameChunker`,
   `settings.outbound_frame_ms`, default 20ms): outbound mu-law audio is re-chunked to
   ~20ms frames regardless of the TTS provider's internal chunk size, matching Twilio/
   Exotel's documented per-message convention and bounding how much audio-equivalent
   time can pass between two cancellation-checkpoints in the speaking loop — i.e. how
   quickly a barge-in can actually stop new audio going out.
6. **Explicit deadlines** (Section 23): `settings.llm_timeout_seconds` /
   `stt_timeout_seconds` wrap the LLM and STT calls in `asyncio.wait_for`. A timeout or
   any other transport failure degrades to the same safe fallback response the
   validator produces for malformed output — it never leaves a turn (or the whole
   media WebSocket loop) hanging. This was a real gap before: neither call had any
   error handling at all in the hot path.
7. **Silence no longer means dead air forever.** If STT produces nothing usable for
   `settings.max_consecutive_empty_turns` turns in a row (silence, noise, a timeout),
   `voice/session/session.py` proactively speaks the script's configured fallback line
   instead of waiting indefinitely.
8. **Bounded inbound audio buffer** (Section 16, `settings.max_pending_inbound_audio_bytes`):
   `VoiceSession.handle_inbound_audio` now drops the oldest buffered audio rather than
   growing without bound if VAD/STT processing ever falls behind real time. Doesn't
   trigger in steady state; it's the backpressure guard the spec calls for.
9. **One DB session per turn, not one per tool call.** `run_turn` now takes an
   already-open `AsyncSession` instead of a session-factory callable that got invoked
   fresh for every tool invocation — one connection-pool checkout per turn.
10. **Turn persistence is fire-and-forget** (`orchestrator/engine.py:
    _persist_turn_fire_and_forget`) on its own short-lived session, never the turn's
    transactional one (that session's lifetime is owned by the caller and closes right
    after `run_turn` returns — sharing it with a background write would race the close).
    Note: only the agent's side of each turn is persisted as a `Turn` row today: the
    customer's transcribed text isn't yet, and `transcript_segment` rows aren't
    populated at all. Not a latency concern (writes are already off the critical path)
    — just an honest gap, worth closing separately from this pass.
11. **Endpointing default tightened** from 700ms to 600ms end-of-turn silence — still
    within the client-approved 550-800ms band (Section 17), just at the tighter end,
    since every ms there is dead air before the agent responds.

### Not done in this pass — the next highest-leverage latency item

**Migrate STT off the buffered-per-utterance REST call to OpenAI's Realtime API in
transcription-only mode.** This remains the single largest fixed cost in the pipeline:
today, ending a turn means waiting `vad_end_of_turn_ms` of silence, THEN uploading and
transcribing the whole buffered utterance via one blocking REST call
(`speech/stt/openai.py`), before the LLM call even starts. A streaming transcription
session would collapse most of that into "the final transcript is ready almost
immediately after VAD says the customer stopped talking." This wasn't done in this pass
because it means writing the Realtime API's WebSocket event protocol (session config,
`input_audio_buffer.append`, `conversation.item.input_audio_transcription.*` events)
from documentation without a live key in this environment to verify exact field names
against — the same reasoning that kept `telephony/exotel.py`'s streaming-applet wiring
flagged rather than silently assumed. `STTProvider`/`STTStream` (`speech/stt/base.py`)
were designed specifically so this swap doesn't touch any caller when it's done.

## What's a clearly-marked placeholder (do not use for a real call)

- `config/prompts/system_prompt.yaml`'s `identity_disclosure` — whether/how to
  disclose this is an AI, per local regulation/company policy. **Confirm before any
  real call.**
- `config/scripts/product-a.yaml` and `config/qualification/rules.yaml` — illustrative
  Amazon-selling qualification script/rules, not the client's actual script or
  thresholds.
- `.env.example` — no real credentials anywhere, obviously.
- `telephony/freejun.py` — interface only, see above.
- `MAX_ATTEMPTS` in `workers/retry.py`, retry cap in `services/call_worker/dispatch.py`
  — placeholder retry policy, needs client confirmation (Section 19/33).
- Call window / concurrency / max call duration defaults in `.env.example` and
  `config/settings.py` — placeholders, Section 33 lists these as things to confirm,
  not assume.
- `apps/api/deps.py:get_current_tenant_id` — trusts an `X-Tenant-Id` header with no
  actual authentication. This is a structural placeholder so tenant scoping is already
  threaded through every route; it is NOT security and must be replaced before this
  API is reachable from anywhere untrusted.

## What's designed but not yet run against something real

- No real Postgres has been started in this environment (Docker Desktop wasn't
  running). The schema/migration/repositories have not been exercised against a live
  DB — do that first thing next session (`docker compose up -d db && alembic upgrade
  head && pytest`).
- No OpenAI API key has been used here — the STT/TTS/LLM adapters are written against
  the documented SDK surface but not smoke-tested against a real key. This specifically
  includes `llm/openai.py:propose_stream` (streaming + strict `json_schema` together) —
  see "Latency & realtime" above; `settings.enable_speculative_tts` stays off until this
  is confirmed against a real key.
- No real phone call has been placed. `apps/api/routers/media.py` implements the
  Twilio/Exotel-shaped WebSocket protocol from documentation; it has not been proven
  against an actual Twilio/Exotel account.
- Exotel's exact App-Bazaar/streaming-applet wiring needs confirmation against the
  client's actual Exotel account (see `telephony/exotel.py` module docstring) —
  everything else in that adapter is against Exotel's stable documented Connect API.

## Deliberately not built yet (per the architecture spec, Sections 26/27)

- LangGraph / `orchestrator/langgraph/` — not created. The MVP's conversation graph
  (Section 3's states) doesn't yet have branching complex enough to justify it over the
  plain state machine, and there's no existing client code using it. Revisit this the
  moment client code arrives with a LangGraph-based orchestration already in it —
  **preserve that, don't replace it** — or if qualification logic grows meaningfully
  more branchy than the current script.
- MCP, RAG, Kafka, Temporal, Kubernetes, self-hosted LLMs, multi-agent architecture —
  none of these are needed at 100-200 calls/day and none are built.
- Google Sheets/CRM/webhook export — CSV only for now (Section 21).
- The full replay/evaluation harness (`evaluation/replay/`) — persona definitions exist
  (`evaluation/personas/personas.py`) but the harness that drives them through
  `ConversationEngine` and asserts on outcomes is not built yet.
- True streaming STT partials (would require the OpenAI Realtime API's transcription
  mode) — current adapter is buffered-per-utterance; see its docstring for the
  tradeoff and how to swap it later.

## Next steps, in priority order

1. Get a real Postgres up and run the migration; fix anything that doesn't match.
2. Smoke-test the OpenAI adapters with a real key (a 5-second `curl`-equivalent script
   per adapter, not a full call yet) — specifically confirm `propose_stream` yields
   incremental deltas before flipping `enable_speculative_tts` on.
3. Get one Twilio (or Exotel) test number working end-to-end for the vertical slice in
   Section 32 — this is the actual "definition of working" per the spec, not a fully
   built platform. Measure real turn-around latency (end of customer speech to first
   audio byte reaching them) on that call before tuning anything further; the changes
   in "Latency & realtime" above are reasoned through and unit-tested but have not been
   measured against a real phone call yet.
4. Migrate `speech/stt/*` to the OpenAI Realtime API's transcription mode — see
   "Not done in this pass" above; this is the next highest-leverage latency change.
5. Persist the customer's side of each turn (not just the agent's) and populate
   `transcript_segment` — currently a gap, not a latency concern.
6. When the client sends their real script/qualification rules/system prompt: replace
   the placeholder YAML files. No Python changes should be needed for this alone — if
   they are, that's a sign business logic leaked into code and should be pulled back
   into config.
7. When the client sends existing code: **do not rewrite it.** Audit first (repo,
   telephony, OpenAI integration, STT/TTS, LLM, prompts, tools, state
   management/graph, database, lead ingestion, qualification, exports, deployment,
   tests — Section 29), categorize KEEP/REFACTOR/REPLACE/ADD/REMOVE against what's
   listed above, and update this file with the result before touching anything.

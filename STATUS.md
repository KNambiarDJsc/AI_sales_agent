# Status

Last updated: 2026-09-27 (live-key smoke test pass). Read this before adding anything —
it tracks what's real vs. placeholder, and what happens when client materials (code,
prompts, scripts, credentials, models) arrive.

## Live-key smoke test (2026-09-27)

A real OpenAI API key was configured locally (`.env`, gitignored — never committed) and
used to actually exercise every adapter, not just review the code. Results:

- **Auth**: works.
- **`OpenAILLMProvider.propose()` (non-streaming structured output)**: **found and
  fixed a real bug that would have broken every single LLM call in production.**
  `orchestrator/schema.py`'s `AGENT_RESPONSE_JSON_SCHEMA` declared `extracted_facts`
  and `tool_call.arguments` as `{"type": "object", "additionalProperties": true}` —
  OpenAI's strict `json_schema` mode rejects that outright ("`additionalProperties` is
  required to be supplied and to be false"), so every real call would have 400'd before
  this fix. Fix: both fields are now sent on the wire as JSON-encoded *strings*
  (strict mode has no true open-ended/map type), decoded transparently by a
  `field_validator(mode="before")` on `ToolCallProposal`/`AgentResponseProposal` — every
  other caller still works with plain dicts, unaware of the wire-format detail.
  Re-verified against the live key after the fix: works, matches the schema exactly.
  7 new regression tests in `tests/unit/test_schema.py`, including the exact wire
  shape the live API returns.
- **`OpenAILLMProvider.propose_stream()`**: confirmed streaming genuinely yields
  incremental deltas alongside strict `json_schema` (49 chunks for a short response,
  first chunk well before the object finished). This was the explicit open risk behind
  `settings.enable_speculative_tts` — **now resolved with real evidence**, including a
  full `ConversationEngine.run_turn()`-level check (not just the raw provider) showing
  the speech callback fires ~250ms before full validation completes, with text that
  matches exactly. Default flipped to **on**.
- **`OpenAITTSProvider.synthesize_stream()`**: works (first audio byte in ~2.6-3.8s,
  which is on the slow side for a single short sentence — worth watching once this is
  on a real call, though variance across two runs suggests some of that is ordinary
  API latency jitter rather than something in our code).
- **`OpenAISTTProvider`**: works. Fed it the *actual TTS output from this same test*,
  resampled 24kHz→16kHz through our own `voice/audio/processing.py`, and got back a
  near-exact transcript — a real, if narrow, end-to-end proof that our own audio
  pipeline code (not just the OpenAI SDK calls) is correct.

**Not yet tested**: a real phone call (still needs Twilio/Exotel credentials + a live
number), TTS sentence-pipelining against real (not fake) concurrent API calls, and STT
under actual telephony-quality (mu-law 8kHz origin, not TTS-synthesized) audio.

**Housekeeping**: the `.env` this was tested with is local to this environment only
(gitignored, never committed — confirmed). The key was pasted directly into chat by the
user; **it should be rotated in the OpenAI dashboard** regardless of test outcome, since
it's now sitting in plaintext in a conversation transcript outside this repo's control.

## Real outbound call attempt (2026-09-27) — blocked by Twilio trial restrictions, not a bug here

Configured real Twilio trial credentials, ran the app (`uvicorn`) exposed publicly via a
Cloudflare quick tunnel (no account needed — `cloudflared tunnel --url http://localhost:8000`;
ngrok was tried first but current versions require signup), created a real
tenant/campaign/lead in the live Postgres from the previous section, and called
`services/call_worker/dispatch.py:dispatch_next_call` directly — the exact function the
production worker loop uses, not a special test path.

**Result**: Twilio rejected the call with HTTP 400 "Invalid or disallowed parameters
provided - trial accounts have limited parameter access." Confirmed via Twilio's own
docs, not guessed: trial accounts can **only** place outbound calls using Twilio's own
fixed template webhooks (the same ones shown in the Twilio Console's "Try Voice"
tester), not custom inline TwiML or a webhook URL pointing at your own server — which
is exactly what `telephony/twilio.py`'s `<Connect><Stream>` TwiML needs to do for our
media pipeline to run at all. This is a hard platform restriction, confirmed against
[Twilio's trial docs](https://www.twilio.com/docs/usage/trials/try-out-voice) and
[error 10002](https://www.twilio.com/docs/api/errors/10002) — not a code bug, and not
something worth working around with a throwaway inbound-call test path, since that
wouldn't validate the actual outbound-dispatch flow the client needs anyway.

**What this run did validate, for real**: the dispatch → `CallAttempt` → telephony
adapter → error-handling path all worked exactly as designed — the failure was recorded
as a `CallAttempt` row with `status='failed'` and the real Twilio error message, and
`dispatch_next_call` returned `False` cleanly rather than raising or crashing anything
(Section 23: never leave a failure unhandled). Test rows cleaned up afterward.

**To actually place a real call**: upgrade the Twilio account from trial (removes the
parameter restriction entirely — no code changes needed, the adapter is already correct)
or provide different, non-trial credentials.

**Left running for a quick retry** (not committed/persisted, gitignored `.env`):
`uvicorn` on `localhost:8000` and a Cloudflare quick tunnel at
`https://carl-renewable-displays-inclusive.trycloudflare.com` → `localhost:8000`. Both
are ephemeral (the tunnel URL changes every time `cloudflared` restarts) — if a session
picks this back up later and either isn't responding, just restart them.

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
- **Tests** (`tests/unit/`): 51 real, running unit tests (no external services needed)
  covering lead import/normalization, the state machine's transition rules, the
  LLM-output validator's fallback behavior (including the granular tool-vs-state
  distinction), qualification scoring, turn-taking/endpointing timing, the speculative
  streaming extractor's safety gating, the engine's speech-callback contract (never
  double-speaks, even across simulated mid-stream failures), the outbound frame
  chunker, the sentence splitter, an integration-style proof that TTS sentence
  pipelining is genuinely concurrent (not just sequential-looking) plus that barge-in
  cancellation leaves no leaked tasks or stray audio, and that both sides of a turn
  (customer + agent) get persisted correctly (with a fake DB session — no live Postgres
  needed). Run: `pytest tests/unit`.

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
3. **Speculative TTS start** (`settings.enable_speculative_tts`, default **on** as of
   the live-key smoke test): streams the LLM's structured-output JSON
   (`llm/openai.py:propose_stream`) and starts speaking as soon as the `speech` field
   has fully arrived — before `tool_call`/`extracted_facts` finish streaming — instead
   of waiting for the whole object. Safety: gated on the `state` field having already
   passed the same `is_transition_allowed` check the non-streaming path uses
   (`orchestrator/streaming.py:SpeculativeTurnExtractor`); if state is invalid, nothing
   is spoken early. `orchestrator/engine.py` guarantees the speech callback fires
   *exactly once* per turn even if the stream fails partway through after speech was
   already spoken (tested in `tests/unit/test_engine_streaming.py`, including the
   "already spoke, stream then dies" edge case). Confirmed against a real key (see
   "Live-key smoke test" above): streaming does arrive incrementally alongside strict
   `json_schema` (49 chunks for a short response), and a full
   `ConversationEngine.run_turn()` check showed the callback firing ~250ms before full
   validation with text matching exactly. Still watch for
   `speculative_tts_mismatch`/`speculative_tts_error` in logs in production — one
   account, one session is not a load test.
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
    _persist_turns_fire_and_forget`) on its own short-lived session, never the turn's
    transactional one (that session's lifetime is owned by the caller and closes right
    after `run_turn` returns — sharing it with a background write would race the
    close). Persists **both sides** of each turn now — customer and agent — as `Turn` +
    `TranscriptSegment` rows (tested in `tests/unit/test_engine_persistence.py`, with a
    fake session so it needs no live Postgres). This closes what had been a flagged gap
    (only the agent's side was recorded); `stt_confidence` on the customer's segment is
    still not populated — `run_turn` only receives the transcribed text, not the full
    `TranscriptResult` with confidence, and threading that through wasn't done in this
    pass.
11. **Endpointing default tightened** from 700ms to 600ms end-of-turn silence — still
    within the client-approved 550-800ms band (Section 17), just at the tighter end,
    since every ms there is dead air before the agent responds.

### Realtime STT migration (2026-09-28) — done, verified against a live key, off by default

The item above was flagged as "the next highest-leverage latency change" and deferred
for lack of a live key to verify the wire protocol against. That blocker is gone (see
"Live-key smoke test"), so this pass built and verified it for real rather than leaving
it flagged again.

**What it is**: `speech/stt/openai_realtime.py` (`OpenAIRealtimeSTTProvider`/
`OpenAIRealtimeSTTStream`) — genuine streaming transcription over OpenAI's Realtime API,
instead of `speech/stt/openai.py`'s buffered-per-utterance REST call. Partial
transcripts arrive while the customer is still talking; the final transcript is ready
almost immediately after our own endpointing calls `receive_final()`, instead of only
then starting a whole-utterance upload+transcribe round trip.

**The protocol was confirmed empirically, not assembled from docs alone** — OpenAI's
own documentation disagreed with itself across pages while researching this (a search
result mentioned `?intent=transcription` in the connection URL; the dedicated
client-events/session reference pages omit it entirely and only show `?model=...`).
Connected to the live API directly to settle it: without `?intent=transcription` (or a
`model=` param) the socket closes immediately with a `missing_model` error; with it,
everything works exactly as `openai_realtime.py`'s docstring now documents in detail
(session.created → session.update with `turn_detection: null` → input_audio_buffer.append
→ manual input_audio_buffer.commit → delta events → a completed event with the final
transcript). Also confirmed empirically: **the audio-rate floor is 24kHz** — 16kHz (our
own VAD's rate) is rejected outright ("integer below minimum value... Expected a value
>= 24000"). Verified twice: once with a standalone exploratory script probing the raw
protocol, once again with the actual `OpenAIRealtimeSTTProvider`/`OpenAIRealtimeSTTStream`
classes end-to-end (synthesized real speech via our own `OpenAITTSProvider`, fed it
through, got back an accurate transcript) — the second run is what actually matters;
the first was protocol discovery.

**This did require touching a caller**, correcting what the previous pass's note
claimed ("designed so this swap doesn't touch any caller") — that was true at the
interface-method level (`send_audio`/`receive_partial`/`receive_final`/`close` didn't
change), but not at the sample-rate level: `voice/session/session.py` used to feed the
STT stream the exact same 16kHz frames it fed its own VAD, which silently assumed every
STT backend accepts 16kHz. It now resamples independently to `STTProvider.
input_sample_rate_hz` (a new attribute, default 16000, overridden to 24000 by the
realtime adapter) for the STT feed, decoupled from the VAD's fixed 16kHz feed — both
resampled straight from the same mu-law source, not one from the other. This is still a
small, contained change (one new attribute, ~10 changed lines in `handle_inbound_audio`),
not a rewrite, and every existing test still passes.

**Selection**: `settings.stt_backend: Literal["buffered", "realtime"]`, read via
`speech/stt/factory.py:get_stt_provider()` (same pattern as `telephony/factory.py`).
Wired into `apps/api/routers/media.py`. **Default stays `"buffered"`** — the live
verification here was one account, one session, not a load test or a real phone call;
flip it once that's been proven, the same caution as `enable_speculative_tts` before its
own live verification.

6 new unit tests (`tests/unit/test_openai_realtime_stt.py`) against a fake websocket
connection replaying the exact event shapes observed live — session.update payload
correctness (including that `turn_detection` is disabled and the rate is the confirmed
floor), rejection handling, base64 audio encoding, partial accumulation, final-transcript
retrieval + manual commit, and graceful timeout if nothing arrives. 64 tests total.

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

- ~~No real Postgres has been started~~ — done. `docker compose up -d db && alembic
  upgrade head` ran against a real Postgres 16 container; all 17 tables + `alembic_version`
  landed correctly (verified with `\dt`). Also exercised the actual repository/service
  code (not raw SQL): tenant/campaign creation, lead import with real phone
  normalization + dedupe (a re-import correctly skipped both rows as existing), and
  `LeadRepository.claim_next_pending`'s `FOR UPDATE SKIP LOCKED` claim (the exact
  mechanism the call worker depends on for concurrency safety) — claimed leads in
  order, correctly returned `None` once exhausted. Test rows cleaned up afterward.
  **Found and fixed a real, unrelated environment issue along the way**: the
  docker-compose default host port 5432 silently collided with a native Postgres
  already running on the dev machine (TCP connected fine, auth failed because it was
  hitting the wrong server) — moved to port 55434 in `docker-compose.yml`/`.env.example`/
  `config/settings.py`'s default (55432 was also taken by an unrelated local project's
  container). Not yet done: an actual production-scale load/concurrency test, and a
  restart-durability check (the container has `restart: unless-stopped`, untested
  across a real host reboot).
- ~~No OpenAI API key has been used here~~ — done, see "Live-key smoke test" above. All
  three adapters (STT/TTS/LLM, including streaming) verified working against a real
  key; one real bug found and fixed in the process (the strict-`json_schema` issue).
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
- ~~True streaming STT partials~~ — built and live-verified; see "Realtime STT
  migration" above. Default backend is still `"buffered"` pending a real phone call to
  prove `"realtime"` end-to-end (not just one verification session).

## Next steps, in priority order

1. ~~Get a real Postgres up and run the migration~~ — done; see "What's designed but
   not yet run against something real" above.
2. ~~Smoke-test the OpenAI adapters with a real key~~ — done; see "Live-key smoke test"
   above. Rotate that key in the OpenAI dashboard before relying on it further — it was
   pasted into a chat conversation, which is outside this repo's control.
3. Get one Twilio (or Exotel) test number working end-to-end for the vertical slice in
   Section 32 — this is the actual "definition of working" per the spec, not a fully
   built platform. **Attempted with a Twilio trial account (see "Real outbound call
   attempt" above) and blocked by trial restrictions, not a code issue** — upgrade the
   Twilio account (or use non-trial credentials) and retry; the server + tunnel are
   already set up and left running for a quick retry. Measure real turn-around latency
   (end of customer speech to first audio byte reaching them) on that call before tuning
   anything further; the changes in "Latency & realtime" above are reasoned through and
   unit-tested but have not been measured against a real phone call yet.
4. ~~Migrate `speech/stt/*` to the OpenAI Realtime API's transcription mode~~ — done and
   live-verified; see "Realtime STT migration" above. Set `STT_BACKEND=realtime` and
   confirm it on an actual phone call before flipping the default.
5. ~~Persist the customer's side of each turn~~ — done; see "Latency & realtime" #10.
   Still open: thread STT confidence through to the customer's `transcript_segment` row.
6. When the client sends their real script/qualification rules/system prompt: replace
   the placeholder YAML files. No Python changes should be needed for this alone — if
   they are, that's a sign business logic leaked into code and should be pulled back
   into config.
7. When the client sends existing code: **do not rewrite it.** Audit first (repo,
   telephony, OpenAI integration, STT/TTS, LLM, prompts, tools, state
   management/graph, database, lead ingestion, qualification, exports, deployment,
   tests — Section 29), categorize KEEP/REFACTOR/REPLACE/ADD/REMOVE against what's
   listed above, and update this file with the result before touching anything.

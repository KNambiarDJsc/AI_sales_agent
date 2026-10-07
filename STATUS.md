# Status

Last updated: 2026-10-02 (Exotel media route pass). Read this before adding anything —
it tracks what's real vs. placeholder, and what happens when client materials (code,
prompts, scripts, credentials, models) arrive.

## Local AI backup — works without an OpenAI key (2026-10-07)

`AI_BACKEND=auto` (default): each stage uses OpenAI while its key works and switches
to a local model on its own — mid-call, same turn — when OpenAI refuses for an
account reason (missing/invalid/expired key, no credits) or the network is down;
OpenAI is retried after 5 min. `local` = never call OpenAI; `openai` = OpenAI only.
`GET /health/ai` shows which backend each stage uses and whether the backup is ready.
Setup on a new machine: `pip install -r requirements-local.txt`, install Ollama, then
`python scripts/setup_local_models.py`.

| Stage | Local model | Measured here (Core Ultra 7 256V, 16 GB, Arc iGPU) |
|---|---|---|
| STT | Moonshine base, ONNX (`useful-moonshine-onnx`) | 0.14–0.55 s per turn (OpenAI ~0.8 s) |
| TTS | Kokoro-82M, ONNX (`kokoro-onnx`), voice af_heart | first audio 0.4–1.0 s (OpenAI ~0.9 s) |
| LLM | llama3.2:3b in Ollama (on the Arc GPU) | reply ready 2.3–4.9 s (OpenAI gpt-4.1-mini ~1.0 s) |

End-to-end in the real `/demo` page, fully local: **2.9–6.2 s** from the end of the
customer's speech to agent audio (OpenAI path: ~2.9 s). Speech is faster than OpenAI;
the LLM on an integrated GPU is the bottleneck. A machine with a discrete GPU would
change this — re-run the model comparison there.

LLM chosen by running four scripted calls through the real engine: llama3.2:3b 3/4
(2.2 s median); qwen2.5:1.5b and qwen3:1.7b 2/4; qwen3:4b 2/4 at ~3.8 s; qwen2.5:3b
excluded (research-only licence). phi4-mini and gemma3:4b were downloaded but their
run was stopped by the OS for low memory — not yet compared.

Guardrails added because small local models broke them (all config-driven, all
apply to every backend unless noted, all tested):
- **DNC backstop**: configured phrases (`dnc_phrases` in the script YAML) record DNC
  and end the call without asking the LLM. With the *local* LLM this is the only way
  to DNC — its DNC guesses are refused (it suppressed customers who'd asked for a call
  back). OpenAI models' DNC judgement is still honoured.
- **Hang-up only from a closing state** (END, DO_NOT_CALL, or a state whose only exit
  is END — derived from the script).
- **Nothing changes before the customer speaks** (opening line / silence re-prompt):
  state pinned, no tools — a local model proposed DO_NOT_CALL + mark_dnc on the greeting.
- **Intent first**: the model classifies the customer's reply into the script's own
  transition labels before choosing the state; without it every local model stayed
  in INTRO forever. Prompt now spells out label → state, and that the lead is the
  person being called (a model introduced itself with the customer's name).
- `Turn.intent` truncated to its 100-char column (a long intent used to make the
  whole turn's transcript write fail).

Also: the browser demo now captures raw 16 kHz PCM (AudioWorklet) instead of webm —
what both STT backends take, and no encode/decode step.

**Known gaps (local backend)**: the local LLM sends `create_qualification` with empty
arguments, so qualification isn't recorded on the local path; it repeats itself more
than OpenAI; latency above. Note `pip install moonshine` is an unrelated satellite-
imagery package — the speech model is `useful-moonshine-onnx`.

## Latency + browser demo pass (2026-10-03)

Measured end-to-end in the real `/demo` page (Chrome, push-to-talk; release of the
button → first agent audio actually playing): **2.5–3.8 s, median ≈2.9 s** over 13
turns, vs. several seconds more before (the page waited for the *whole* reply to be
synthesized before playing anything). Per turn ≈ STT 0.7–1.3 s · LLM 0.8–1.6 s · TTS
first audio 0.8–1.0 s — each at the floor of the fastest model measured for it.

- Models re-chosen by benchmark on this project's prompt/schema (`config/settings.py`
  has the numbers): LLM gpt-4o → **gpt-4.1-mini** (speech ready 1.96 s → 0.84 s), STT →
  **gpt-4o-mini-transcribe**, TTS stays gpt-4o-mini-tts (tts-1 was slower). Realtime
  streaming STT measured only ~0.1 s faster than REST, so the demo keeps REST.
- One shared, pooled OpenAI client per process (`llm/openai_client.py`); system prompt
  reordered static-first (prompt-cache friendly); one LLM deadline per turn (a timed-out
  stream no longer gets a second full timeout — that was the 17 s silence); TTS hands
  over audio in 25 ms pieces instead of 85 ms.
- English only: STT pinned to `STT_LANGUAGE=en`, prompt rule to always reply in English
  (verified: Hindi and Spanish speech both got English replies).
- Demo rewrite (`apps/api/routers/demo.py`): turns strictly serialised, newest utterance
  wins, barge-in on button press, streaming playback, accidental clicks/silent
  recordings discarded, one MediaRecorder per recording (a shared buffer is how two
  quick presses merged), per-turn timing shown. Verified in Chrome: double press → one
  reply; barge-in mid-reply → zero overlapping audio; no page errors.
- Prompt rules (config only): short replies, no invented names/placeholders, don't end
  the call over a question (the model hung up when asked "who are you calling from?").

**Sub-0.3 s is not reachable with this architecture**: STT, LLM and TTS are three
sequential network calls, and the fastest models measured for each take ~0.7–0.9 s on
their own. Options that would go further, each a decision rather than a tweak: stream
mic audio to realtime STT during the press (~0.1–0.3 s); start TTS on the first sentence
while the LLM is still writing (changes the engine's one-callback-per-turn contract);
or a speech-to-speech model (OpenAI Realtime), which typically answers in well under a
second but speaks before our validator can check the reply — conflicts with "the LLM
proposes; the application decides" (CLAUDE.md rule 2).

## FreJun (Teler) integration (2026-10-03) — active provider; code path proven, real call blocked on account setup

FreJun/Teler replaces Exotel as the active provider (Exotel code left intact, unused).
Built from FreJun's official docs (frejun.com/docs/teler/) and official SDK
(github.com/frejun-tech/teler-py) — every request/message shape in
`telephony/frejun.py` is traced to one of them. Inbound calls to the Teler number get
a clean `hangup` from `/flow/frejun/inbound` (the Voice App's required Incoming Call
URL); only outbound calling is implemented.

**Call path**: worker/`scripts/frejun_call.py call` → `dispatch_next_call` (attempt row
now committed *before* dialling) → `POST /voice/calls/initiate` (X-API-Key) → callee
answers → Teler `POST /flow/frejun/{call_attempt_id}` (`apps/api/routers/flows.py`)
→ we return a `stream` Call Flow → Teler opens `wss://…/media/frejun/{call_attempt_id}`
(`apps/api/routers/media.py`) → `start` (call_id checked against the attempt) → `audio`
(base64 PCM16 8 kHz mono) → existing `VoiceSession` pcm16 path → outbound
`{"type":"audio","audio_b64","chunk_id"}` / `{"type":"clear"}`. Status webhooks: JSON
to `/webhooks/frejun`, HMAC-SHA256 verified with `FREJUN_SECRET` (5-min replay window).
Hangup: `POST /voice/calls/{id}/hangup` with `Idempotency-Key`.

**Proven** (not just compiled): `scripts/frejun_call.py check` passes every code-side
check through the public tunnel (signed/unsigned webhooks, flow JSON, WSS handshake,
VoiceSession → LLM → TTS → Teler-format audio). `simulate all` ran three real-speech
calls in FreJun's wire protocol through the tunnel: interested lead went INTRO →
PERMISSION → DISCOVERY → QUALIFICATION → INTERESTED and recorded `qualified` from real
facts + quoted evidence; DNC wrote suppression and suppressed the lead; callback stored
one callback (LLM proposed it twice — now idempotent) and set lead `callback_scheduled`.

**Blocked**: the FreJun account has 0 numbers and 0 Voice Apps (API-verified), so no
real call can be placed. Needs: a Teler number, a Voice App with that number attached
(Incoming Call URL `…/flow/frejun/inbound`, Call Status URL `…/webhooks/frejun`), the
Voice App's webhook secret in `FREJUN_SECRET`, `FREJUN_FROM_NUMBER`.

**Fixed in the same pass** (found live in the browser demo):
- LLM never saw tool argument schemas or the rules' fact keys → `create_qualification({})`
  was always rejected. Prompt now renders each allowed tool's Pydantic schema and the
  fact keys from `config/qualification/rules.yaml` (still validated by the tool/scorer).
- `schedule_callback` inserted a duplicate row when re-proposed → one pending callback
  per conversation, updated in place.
- Leads stayed `in_progress` forever → `services/call_worker/lifecycle.py:finalize_call`
  (media teardown + terminal webhooks, idempotent): `completed` / `callback_scheduled`
  / `suppressed` / `failed` (never connected — retry sweep requeues), with
  `CallAttempt.outcome` and the final conversation state recorded.
- Earlier: prompt stuck on INTRO, failed tool crashing the turn, browser playback,
  naive-timestamp columns (callback/export) — see git diff on `naman-development`.
- Webhook route passed `dict(request.headers)` (lowercased keys), so Twilio's
  `X-Twilio-Signature` lookup always failed; now passes the case-insensitive headers.

**Known issues, not fixed**: turn latency 3.5–6 s from end of speech to first agent
audio (buffered STT + LLM + TTS first chunk); replies are long (10–24 s of audio);
600 ms endpointing cut "Please stop calling me. Remove my number…" at the first
sentence; placeholder product_info lets the LLM say "[Your Company Name]"; inbound
calls are hung up, not handled.

## Exotel media WebSocket route (2026-10-02) — built ahead of credentials, found a real routing bug

Preparing for a free Exotel trial signup (see "Real outbound call attempt" and the
Twilio-trial-blocked finding below) surfaced an architectural difference from Twilio
worth building for ahead of time: Exotel's Voicebot/Stream applet is configured with a
URL *once*, at flow-design time in their App Bazaar console — there's no per-call
dynamic TwiML-equivalent to embed a `call_attempt_id` in, the way `telephony/twilio.py`
does. Exotel does support a dynamic-HTTPS-URL option (`{"url": "wss://..."}`), but its
own request parameters aren't documented, so the robust choice — confirmed against
[Exotel's AgentStream docs](https://developer.exotel.com/docs/agentstream/stream-voicebot-applet) —
is a **static** WebSocket URL, correlating each connection to our `CallAttempt` via the
`call_sid` Exotel sends in its own `start` event, matched against the
`provider_call_id` we stored when `create_outbound_call` returned it.

- `apps/api/routers/media.py` refactored: shared setup logic extracted into
  `_resolve_attempt_and_build_session()` (resolve by attempt id OR by provider call
  sid), two thin routes on top of it — `/media/{call_attempt_id}` (Twilio, unchanged
  behavior) and `/media/exotel` (new, static URL + `call_sid` correlation).
- **Found a real routing bug via the new tests, not by inspection**: registering
  `/media/exotel` *after* `/media/{call_attempt_id}` meant Starlette matched
  `/media/exotel` against the UUID-typed path param first (any single path segment,
  including the literal "exotel", structurally fits `{call_attempt_id}` before type
  conversion is attempted) and rejected it for an invalid UUID — the literal route
  never got a chance. Fixed by registering the specific route first; a dedicated
  regression test (`test_twilio_route_still_reachable_after_exotel_route_added_first`)
  guards the ordering.
- 5 new tests (`tests/unit/test_media_exotel.py`): correlation/rejection paths only (no
  OpenAI calls, so these run offline) — the "happy path" (a real call_sid resolving to
  a real CallAttempt and the agent actually speaking) needs a live key and is left to
  manual verification once real Exotel credentials exist, not a per-run test cost.
  These are plain `def` tests, not `async def` under pytest-asyncio, and reset the
  app's DB connection pool before each one — `TestClient.websocket_connect` runs the
  ASGI app in its own background thread with its own event loop, and asyncpg
  connections cannot be reused across event loops; nesting it inside pytest-asyncio's
  loop, or reusing a pooled connection across two different TestClient instances,
  both reproduce this. A real server has exactly one loop for its whole process
  lifetime, so neither failure mode is a production concern — purely a testing-infra
  footgun worth documenting for next time. 74 tests total.
- **Still unverified** (no Exotel credentials yet): the exact `Calls/connect` API
  parameters in `telephony/exotel.py:create_outbound_call` for a pure voicebot call
  (its current shape follows Exotel's "connect two numbers" pattern, which may not be
  the right call for "ring a lead directly into an automated flow with no second human
  leg" — flagged, not fixed, since guessing here without a live account to test against
  would repeat the exact mistake this project has avoided elsewhere). Fix once
  credentials are in hand, the same live-test-and-adjust approach used for Twilio and
  the Realtime STT migration.

## Browser demo (2026-10-01) — a free, no-telephony way to show the real system working

The client's stated telephony requirements (WhatsApp messages, paraphrased: Freejun for
a Bangalore-based Indian number; OpenAI for STT/TTS — already the default; "you don't
get a virtual number without showing ID proof") confirm what was already known: every
telephony path needs either carrier KYC for a real Indian number (Freejun/Exotel) or a
paid account (Twilio — trial accounts can't use the custom webhook/streaming this
architecture needs, see "Real outbound call attempt" below). None of that blocks
demonstrating the actual AI agent, so `apps/api/routers/demo.py` (new) runs the
**exact same orchestration stack** — `ConversationEngine`, the real `product-a` script
and state machine, the real tool registry and qualification engine, real OpenAI
STT/TTS — over the browser's own microphone/speakers instead of a phone line. No
telephony provider, no KYC, no paid account, works today.

- `GET /demo` serves a single-page push-to-talk UI (hold a button, speak, release);
  `WebSocket /demo/ws` receives each utterance as a webm/opus blob (what a browser's
  `MediaRecorder` actually produces), transcribes it via OpenAI directly, runs it
  through `ConversationEngine.run_turn()` exactly like a real call would, and streams
  the synthesized response back as raw PCM16 for the browser to play via the Web Audio
  API.
- Provisions its own throwaway Tenant/Campaign/Lead/CallAttempt/Conversation rows per
  session (get-or-create a "Browser Demo" tenant/campaign, fresh lead each session) —
  this is a demo harness, not a second production transport. When real telephony is
  available, the call path is `apps/api/routers/media.py`, not this file.
- **Verified end-to-end against the live backend** (not just that it boots): connected
  via FastAPI's `TestClient.websocket_connect`, sent a real TTS-synthesized "customer
  reply" WAV (webm wasn't producible without `ffmpeg`, unavailable in this environment —
  a real browser's `MediaRecorder` always produces genuine webm, so this only affected
  the test harness, not `demo.py`'s production code), and confirmed a correct transcript
  came back, followed by a real LLM-driven response with audio.

**Found and fixed a real, separate bug in the process**: when a fallback response
fires (e.g. the LLM proposes a state jump the script doesn't allow and gets correctly
rejected — working as designed), the script's configured fallback line could contain a
literal, never-substituted `{business_name}`-style placeholder
(`config/scripts/product-a.yaml`'s INTRO state has exactly this). Nothing in the
codebase ever implemented template substitution for these — the customer would have
heard the literal token spoken aloud. This would have been immediately, embarrassingly
visible in a live demo. Fixed: `orchestrator/state_machine.py:substitute_placeholders`
(a `str.format_map` with a dict that leaves unknown keys as literal `{key}` rather than
raising, so a script typo degrades gracefully instead of crashing a live call), wired
into every place script text actually reaches the customer or the LLM's prompt
(`orchestrator/validator.py`'s fallback construction, `orchestrator/engine.py`'s
transport-failure fallback and the silence re-prompt, `orchestrator/prompts.py`'s
mandatory-questions rendering). 6 new tests, including a direct regression test against
`product-a.yaml`'s actual fallback text. Re-verified live after the fix: the fallback
now correctly says "Sorry, could you tell me if this is Demo Business?" instead of the
literal placeholder.

**Also learned, worth knowing for later**: Exotel's trial account, unlike Twilio's,
allows full API access and custom call-flow/webhook configuration (it only restricts to
~10 verified numbers, no KYC needed for those) — [confirmed via their
docs](https://developer.exotel.com/docs/getting-started/trial-account). If the client
sets up a free Exotel trial and verifies their own number, the actual telephony path
(not just this browser demo) could be tested for free before any KYC/paid commitment —
worth proposing as the next concrete step once there's appetite for it.

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
- **Tests** (`tests/unit/`): 69 real, running unit tests (no external services needed;
  run `pytest tests/unit` to get the current count — this number goes stale fast)
  covering lead import/normalization, the state machine's transition rules (including
  script placeholder substitution), the LLM-output validator's fallback behavior
  (including the granular tool-vs-state distinction), qualification scoring,
  turn-taking/endpointing timing, the speculative
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
retrieval + manual commit, and graceful timeout if nothing arrives. 64 tests as of this
pass (69 after the browser-demo pass's placeholder-substitution fix, see above).

## What's a clearly-marked placeholder (do not use for a real call)

- `config/prompts/system_prompt.yaml`'s `identity_disclosure` — whether/how to
  disclose this is an AI, per local regulation/company policy. **Confirm before any
  real call.**
- `config/scripts/product-a.yaml` and `config/qualification/rules.yaml` — illustrative
  Amazon-selling qualification script/rules, not the client's actual script or
  thresholds.
- `.env.example` — no real credentials anywhere, obviously.
- ~~`telephony/freejun.py` — interface only~~ — replaced by a real `telephony/frejun.py`
  (FreJun/Teler), see the FreJun section at the top.
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
   Twilio account, or set up a free Exotel trial (confirmed to allow custom webhooks —
   see "Browser demo" above), and retry. In the meantime, `/demo` (see "Browser demo"
   above) demonstrates the real orchestration/qualification stack today without any
   telephony account. Measure real turn-around latency (end of customer speech to first
   audio byte reaching them) on an actual phone call before tuning anything further; the
   changes in "Latency & realtime" above are reasoned through and unit-tested but have
   not been measured against a real phone call yet. Note: the uvicorn server + Cloudflare
   tunnel from the previous pass are no longer running (killed by the host OS for memory
   pressure between sessions, and this session's `/demo` work used fresh ones) — start
   both again before sharing a link.
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

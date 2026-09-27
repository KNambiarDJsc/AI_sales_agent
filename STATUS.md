# Status

Last updated: 2026-09-27. Read this before adding anything — it tracks what's real vs.
placeholder, and what happens when client materials (code, prompts, scripts,
credentials, models) arrive.

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
  active script's allowed transitions/tools before anything acts on it. Malformed or
  disallowed output always falls back to the script's own fallback line and current
  state; it never crashes and never silently does something the script didn't allow.
  `orchestrator/engine.py` wires prompts → LLM → validator → tools → state transition
  into one `run_turn()` call.
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
  real resampling via `audioop.ratecv`, never sample duplication), WebRTC VAD
  (`vad/vad.py`), a config-driven endpointing state machine separate from VAD
  (`turn_taking/turn_taking.py`), and `VoiceSession` (`session/session.py`) which wires
  all of it together plus barge-in (Section 18: cancel TTS, clear provider audio,
  cancel the speaking task on new customer speech).
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
- **Tests** (`tests/unit/`): real, running unit tests (no external services needed) for
  lead import/normalization, the state machine's transition rules, the LLM-output
  validator's fallback behavior, qualification scoring, and turn-taking/endpointing
  timing. Run: `pytest tests/unit`.

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
  the documented SDK surface but not smoke-tested against a real key.
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
   per adapter, not a full call yet).
3. Get one Twilio (or Exotel) test number working end-to-end for the vertical slice in
   Section 32 — this is the actual "definition of working" per the spec, not a fully
   built platform.
4. When the client sends their real script/qualification rules/system prompt: replace
   the placeholder YAML files. No Python changes should be needed for this alone — if
   they are, that's a sign business logic leaked into code and should be pulled back
   into config.
5. When the client sends existing code: **do not rewrite it.** Audit first (repo,
   telephony, OpenAI integration, STT/TTS, LLM, prompts, tools, state
   management/graph, database, lead ingestion, qualification, exports, deployment,
   tests — Section 29), categorize KEEP/REFACTOR/REPLACE/ADD/REMOVE against what's
   listed above, and update this file with the result before touching anything.

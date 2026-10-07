# voice-sales-agent

AI outbound voice sales qualification system. Built for a 4–5 day MVP, structured so
client-specific scripts, qualification rules, prompts, telephony provider and any
existing client code/models can be swapped in later without touching the core engine.

## Status (see `STATUS.md`)

This is the **initial scaffold**: the architectural skeleton, interfaces, database schema,
config system, and the first vertical slice wiring are in place. No client script, no
client qualification rules, no client credentials, and no client code have been provided
yet — those are stubbed as clearly-marked placeholders (`config/scripts/product-a.yaml`,
`config/qualification/rules.yaml`, `.env.example`). **Do not treat any placeholder text as
the real script — it exists only so the pipeline runs end-to-end.**

Read `STATUS.md` before adding anything — it tracks what's real, what's a stub, and the
next steps in priority order. When client materials (code, prompts, scripts, credentials)
arrive, they get audited against `STATUS.md`'s KEEP/REFACTOR/REPLACE/ADD plan — see
`CLAUDE.md` at the repo root for the standing rule.

## Layout

See `CLAUDE.md` for the full directory map and architectural rules (media layer vs.
orchestration layer, provider abstractions, tool authorization boundary, etc). Short version:

```
apps/api/            FastAPI app: campaign/lead/webhook endpoints
services/             campaign, lead_import, call_worker domain logic
voice/                realtime media: session, audio, VAD, turn-taking (NOT wired to a
                       real telephony stream yet — see STATUS.md)
telephony/            TelephonyProvider + FreJun (Teler)/Exotel/Twilio adapters
speech/               STTProvider / TTSProvider + OpenAI adapters
llm/                  LLM provider abstraction + OpenAI adapter
orchestrator/         state machine, prompt assembly, LLM-output validator
tools/                typed internal tool registry (dnc, callback, qualification, ...)
qualification/        config-driven scoring/rules engine
database/             SQLAlchemy models, repositories, Alembic migrations
workers/              call worker loop, retry, export
observability/        tracing + structured logging setup
config/               prompts/, scripts/, qualification/ — versioned, non-code business logic
evaluation/           persona definitions for scripted test calls
tests/                unit/integration/contract/adversarial
```

## Setup

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows
pip install -r requirements.txt
cp .env.example .env              # fill in secrets before running anything real
docker compose up -d db           # Postgres for local dev
alembic upgrade head
uvicorn apps.api.main:app --reload
```

## What is NOT real yet

- FreJun (Teler) is the active telephony provider (`telephony/frejun.py`, built from
  FreJun's official docs/SDK). A real call needs a Teler number attached to a Voice
  App — see `STATUS.md` and `python scripts/frejun_call.py check`.
- No client sales script / qualification thresholds — `config/scripts/product-a.yaml` and
  `config/qualification/rules.yaml` are illustrative placeholders for the Amazon-selling
  qualification use case, marked `PLACEHOLDER — replace with client script`.
- The realtime audio path (`voice/`) has the session/VAD/turn-taking abstractions and an
  Exotel/Twilio-shaped WebSocket media handler, but has not been run against a live call.
- OpenAI STT/TTS/LLM adapters are written against the documented SDK surface but not yet
  smoke-tested with a real API key in this environment.

None of this is faked to look done — see `STATUS.md` for the exact KEEP/REFACTOR/ADD list.

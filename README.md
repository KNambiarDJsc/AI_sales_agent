# Voice Sales Agent

A Python/FastAPI prototype for configurable, voice-based lead qualification. The project pairs a browser voice demo with a stateful conversation engine and explicit validation around tool actions.

## What it demonstrates

- A push-to-talk browser demo at `/demo`, using the same conversation engine, state machine, tool registry, and qualification flow as the call path.
- Structured LLM proposals that are checked against a response schema and the current script state before they can advance the conversation.
- Typed tools with per-state authorization and validated arguments. Tool handlers receive tenant, campaign, lead, and conversation context rather than unrestricted database, shell, or HTTP access.
- Configurable scripts and qualification rules.
- OpenAI speech/model providers with local fallback options when configured.
- OpenTelemetry tracing setup, with OTLP export enabled when an endpoint is configured.

## Demo and verification

The dated [development status notes](STATUS.md) record an end-to-end browser-demo check against the backend and live OpenAI adapter smoke tests. These are project development checks, not an independent audit.

**A real phone call has not been verified.** The browser demo avoids carrier setup and is not evidence of production telephony. The bundled Product A script and qualification rules are illustrative placeholders; replace them with approved client-specific material before use.

## Run locally

Requirements: Python, Docker Compose, and an OpenAI API key for the hosted speech/model path. Local models can be configured separately.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
# Set OPENAI_API_KEY in .env, then start the database and app.
docker compose up -d db
alembic upgrade head
uvicorn apps.api.main:app --reload
```

Open `http://127.0.0.1:8000/demo` and allow microphone access to try the browser flow.

For local speech/model fallback setup, see `requirements-local.txt`, `scripts/setup_local_models.py`, and `.env.example`. For configuration and dated implementation notes, see [STATUS.md](STATUS.md) and [CLAUDE.md](CLAUDE.md).

## Main components

- `apps/api/` — FastAPI routes, including the browser demo.
- `orchestrator/` — conversation state, prompts, streaming, and response validation.
- `tools/` — typed, state-authorized actions.
- `speech/` and `llm/` — provider interfaces and implementations.
- `telephony/` — carrier-facing code; live calling remains unverified.
- `observability/` — logging and tracing setup.
- `database/` — SQLAlchemy models, repositories, and migrations.

## Scope and limitations

This is a prototype and engineering project, not a production-ready calling service, compliance certification, or guarantee of sales results. Do not use the example script, thresholds, or credentials for real campaigns without replacing and reviewing them. Validate the carrier, legal, privacy, and consent requirements for each deployment.

Related work: [OpenMHS](https://github.com/KNambiarDJsc/Open-MHS) explores scoped, auditable actions against simulated devices; no physical hardware has been validated. [Meridian](https://www.meridianhq.in/) is a separate repository-assessment product and does not provide legal advice.

# voice-sales-agent — working rules

This file is the durable memory for this repo: read it before making architectural
changes, whether you're me in a future session or someone else. `STATUS.md` has the
current state (what's real vs. placeholder); this file has the rules that shouldn't
change just because a session ended.

## What this is

An AI outbound voice sales qualification system. A lead sheet goes in, the system
calls leads one by one, has a scripted-but-adaptive conversation, and returns
qualified leads to a human sales team. Current business use case: qualifying whether a
prospect wants to sell their product on Amazon/online — but that is campaign
*configuration*, not something baked into the engine (see below).

## Non-negotiable architectural rules

1. **Business logic is config, not code.** The sales script, qualification
   thresholds, system prompt, and campaign product info live in
   `config/scripts/*.yaml`, `config/qualification/*.yaml`, `config/prompts/*.yaml`.
   If changing a client's script or qualification bar requires touching `.py` files,
   that's a bug — pull the logic back into config.
2. **The LLM proposes; it never decides.** Every LLM turn output goes through
   `orchestrator/validator.py` before anything acts on it: JSON validity, schema
   validity, allowed state transition, allowed tool. Malformed/disallowed output
   always falls back to the script's configured fallback line and the *current*
   state — never crashes, never silently does something ungoverned.
   The LLM specifically never has final say over: qualification outcome (computed by
   `qualification/scoring.py` from facts it reports, not asserted by it), DNC
   enforcement, protected lead fields, or anything outside the typed tool registry.
3. **DNC always wins.** `qualification/scoring.py` checks it first, before any other
   condition. `tools/dnc.py`'s `mark_dnc` and the manual `/leads/{id}/dnc` API both
   write to the same `suppression` table, and every dial-time check
   (`LeadRepository.is_suppressed`) must consult it.
4. **No MCP, no LangChain-just-because-LangGraph, no framework for its own sake.**
   Tools are a plain internal typed registry (`tools/registry.py`). LangGraph is
   allowed *inside* `orchestrator/` if a campaign's conversation gets branchy enough to
   justify it (create `orchestrator/langgraph/` only then) — it must never touch raw
   audio/WebSocket/VAD. The realtime media loop (`voice/`) and the agent orchestration
   loop (`orchestrator/`) stay separate; `voice/session/session.py` only ever calls
   `ConversationEngine.run_turn()` with text in, gets text+end_call out.
5. **Provider abstractions stay real abstractions.** `TelephonyProvider`,
   `STTProvider`, `TTSProvider`, `LLMProvider` are the only things application code
   depends on outside of `telephony/`, `speech/`, `llm/` themselves. Don't invent a
   provider's API from guesswork — `telephony/freejun.py` is the template for "we
   don't have verified docs yet," not a thing to route around.
6. **Don't add infrastructure the current scale doesn't need.** No Kafka, Temporal,
   Kubernetes, Redis (yet), self-hosted LLMs, multi-agent architectures, or RAG. A
   Postgres-backed queue with `FOR UPDATE SKIP LOCKED` (`database/repositories/
   lead_repository.py:claim_next_pending`) is the concurrency model until real
   throughput numbers say otherwise.
7. **Idempotency over cleverness.** Call attempts are keyed on
   `(campaign_id, lead_id, attempt_number)` with a DB unique constraint — never retry
   an outbound call by just calling the provider again; create a new attempt row.
8. **Tools never get raw access.** No tool touches the DB/HTTP/filesystem beyond its
   own narrow Pydantic-validated job (see any file in `tools/`). No tool claims success
   it didn't actually achieve (Section 23 of the original spec: a failed tool call must
   never be reported to the customer as having succeeded).
9. **Never fabricate business requirements.** Placeholders (script wording, thresholds,
   call windows, retry caps, identity disclosure policy, CRM choice) are marked exactly
   that in `STATUS.md` and inline comments — do not quietly firm them up without the
   client confirming.
10. **The speech callback fires exactly once per turn — never zero, never twice.**
    `orchestrator/engine.py:run_turn`'s `on_speech_ready` contract is load-bearing: the
    voice session starts a TTS/telephony task the instant it fires, so calling it twice
    means the agent audibly says two different things for one turn, and never calling
    it means dead air. If you touch `_propose_and_validate`/`_propose_and_validate_streaming`,
    re-run `tests/unit/test_engine_streaming.py` — it specifically covers the "already
    spoke, then the stream died" and "nothing spoken yet, fall back to the plain path"
    branches. Never let a tool call's DB round trip sit between the LLM response
    resolving and this callback firing — that reintroduces the latency this was built
    to remove.

## When client materials arrive (existing code, prompts, scripts, credentials)

Do not rewrite first. Audit: repo structure, telephony integration, OpenAI usage,
STT/TTS, LLM, prompts, tools, state management (state machine or graph), database,
lead ingestion, qualification, exports, deployment, tests. Categorize each piece
KEEP / REFACTOR / REPLACE / ADD / REMOVE against what's already in this repo (see
`STATUS.md`'s inventory), write that plan into `STATUS.md`, then implement. If the
client's code already uses LangGraph, keep it and build `orchestrator/langgraph/`
around it rather than replacing it with the custom state machine.

## Where things live

See `README.md` for the directory map. The one-line version: `apps/api` is the outside
world, `services/` + `workers/` are campaign/lead/call plumbing, `voice/` is realtime
media only, `orchestrator/` + `tools/` + `qualification/` are the agent's brain and its
guardrails, `telephony/` + `speech/` + `llm/` are swappable provider adapters,
`database/` is schema + repositories, `config/` is everything business-specific.

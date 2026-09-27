# Replay harness — not yet built

Planned shape (Section 25): drive `orchestrator.engine.ConversationEngine.run_turn()`
directly with each persona's scripted customer lines (evaluation/personas/personas.py),
skipping real audio/telephony, and assert:

- final qualification outcome matches the persona's `expected_outcome`
- DNC personas always produce a Suppression row
- malformed/adversarial personas never produce a tool call outside the allowed set for
  the current state (orchestrator/validator.py should already guarantee this — this is
  where that guarantee gets tested end-to-end)
- script adherence: mandatory_questions for each visited state were actually asked

This is deliberately deferred past the first vertical slice (Section 32) — see
STATUS.md for current priority order.

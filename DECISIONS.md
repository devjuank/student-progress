# Decisions

Mini-ADRs for the AI Quality Gate. Format: context, decision, alternatives, consequences.

## ADR-001 — Hybrid reviewer: deterministic checks + LLM

**Context.** Some violations are mechanical (committed secrets, failing tests, PII field names in a
log call); others need semantic reasoning (indirect PII leaks, ineffective tests, logic bugs).

**Decision.** Run deterministic checks first and pass their results to the LLM as hints. Deterministic
findings are trusted to block on their own; LLM findings go through the trust policy (`SPEC.md` §6).

**Alternatives.** LLM-only (simpler, but non-deterministic on cases a regex catches 100% of the time);
rules-only (cheap and stable, but misses the semantic cases that matter most here).

**Consequences.** Two sources to merge and dedupe. Higher-confidence blocking on mechanical issues,
lower cost per PR since the LLM does not need to rediscover them.

## ADR-002 — LiteLLM as a library, model chosen by env var

**Context.** The eval compares models, and the provider may change.

**Decision.** Call the LLM through LiteLLM as a Python library; the model comes from `REVIEWER_MODEL`.

**Alternatives.** Provider SDK directly (fewer dependencies, but locks the code to one provider);
LiteLLM proxy server (central keys and budgets, but one more service to run for a 4–5 hour challenge).

**Consequences.** Switching models is a config change. The proxy is documented as the production path.

## ADR-003 — Bot trust policy separate from team severities

**Context.** TEAM-STANDARDS says every S1/S2 blocks merge. A reviewer with false positives that blocks
everything gets ignored or disabled.

**Decision.** Auto-block only deterministic S1/S2 and high-confidence LLM S1; everything else comments
and asks for a human. Thresholds are revisited with eval metrics (`EVAL.md`).

**Alternatives.** Block on every S1/S2 (strict, but noisy); never block (safe, but adds no protection).

**Consequences.** Some real S2 issues will not block automatically and depend on human review until the
eval shows the reviewer is precise enough for that category. The confidence threshold relies on
self-reported confidence, which is poorly calibrated; agreement across N runs is the planned
replacement if eval shows unstable precision.

## ADR-004 — Consistency without temperature: JSON in the prompt, validated with retries

**Context.** The plan asked for a low temperature to get stable reviews. Current Claude models reject
`temperature` (HTTP 400), and LiteLLM's `response_format` / `reasoning_effort` may be translated into
request shapes these models also reject (forced tool choice, fixed thinking budgets).

**Decision.** Send no sampling or effort parameters. Ask for a JSON object in the prompt, validate
each finding against the schema (SPEC §2), drop invalid ones (counted) and retry up to 3 times when
the envelope is invalid. Stability is measured, not assumed: repeated eval runs (Plan B).

**Alternatives.** Anthropic SDK with native structured outputs (schema guaranteed by the API, but
ties the code to one provider, against ADR-002); an older model that still accepts `temperature`
(worse reviewer to satisfy a parameter).

**Consequences.** Some variance between runs is expected and must show up in the eval. The model's
default effort applies; tuning it is a candidate improvement if the eval shows the need.

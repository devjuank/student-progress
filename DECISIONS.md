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

## ADR-005 — CI security: the API key never shares a runner with the PR's code

**Context.** The gate runs `pytest`, which executes the PR's code. Any code in the PR (a test, a
`conftest.py`, a dependency in `requirements.txt`) could read environment variables and exfiltrate the
API key. A PR could also edit `TEAM-STANDARDS.md`, `PII_FIELDS` or the reviewer itself to weaken the
review it gets.

**Decision.** Two jobs on `pull_request` (never `pull_request_target`):
- `tools`: checks out the PR and runs ruff + pytest. No secrets, read-only token, no persisted
  credentials. Only its ruff/pytest results are passed on (as an artifact).
- `review`: has the API key. Runs the reviewer code from the **base** branch, reads the PR's files
  only through git, and never executes them. Standards are read from the base; PII fields are the
  union of base and head, so a PR cannot remove a field to hide a leak.

`block` fails the check (red). `review` passes with a warning and a comment asking for a human, so the
trust policy (ADR-003) has an effect in CI.

**Alternatives.** One job (simpler, but the PR's code runs next to the key); `pull_request_target`
(secrets also on fork PRs, but it is the classic way to leak them when combined with checking out the
PR); no tests in the gate (safe, but loses the deterministic §2 signal).

**Consequences.** The `tools` artifact comes from untrusted code: a PR could fake its own ruff/pytest
results, which it could already do by editing its tests; static checks and the LLM review are
recomputed in the trusted job. PRs from forks get no secrets on `pull_request`: the LLM review fails
and the gate asks for human review instead of approving. The reviewer must be on the base branch
before it can review PRs against it.

## ADR-006 — Prompt changes are justified by the standards, not by the golden-set score

**Context.** The golden set is small (8 PRs, 13 issues) and is the only labeled data. In step 7, a
severity heuristic fixed one false negative and introduced a new false positive; the natural next
edit ("minor test gaps are S3") was written knowing how one ground-truth issue was labeled. Iterating
like that tunes the prompt to these 8 PRs, while the review session tests a PR we have not seen.

**Decision.** A prompt change is accepted only if it can be justified from the text of
TEAM-STANDARDS and would apply the same way to any PR. It is never accepted because it improves the
golden-set metric. The prompt contains no examples or hints drawn from the golden-set PRs. After the
single step-7 iteration, the final severity wording restates §6 and was measured once.
Generalization is measured on new PRs the reviewer was never tuned on.

**Alternatives.** Keep iterating until the metrics are perfect (better numbers, worse reviewer on
unseen PRs); split the 8 PRs into tuning and test sets (too small for either half to mean anything).

**Consequences.** Known failures stay documented in `EVAL.md` instead of being patched. Fixes that
come from ambiguity in the standards (for example, whether logs are a §4 dataset) belong in
TEAM-STANDARDS, not in the prompt.

## Left out, and why

- **LiteLLM proxy, central keys and budgets** — the production path (ADR-002); one more service is
  not worth it for a single repo.
- **OpenTelemetry export (Langfuse/Datadog)** — telemetry is already structured metadata-only JSONL;
  exporting it is a configuration step once a backend exists. In production: GenAI semantic
  conventions and trends for cost/day, latency, error rate, block rate and override rate.
- **Model comparison and more stability runs** — Plan B; the harness supports both
  (`REVIEWER_MODEL`, `--runs N`).
- **Line-level inline PR comments** — a single comment with a table is enough to act on and is
  easy to keep idempotent.
- **Deploying the service, UI, dashboards, fine-tuning** — out of scope for the challenge.

# SPEC — AI Quality Gate

One-page contract for the PR reviewer. Rules come from `TEAM-STANDARDS.md`; this document defines
how the reviewer applies them and how it is measured.

## 1. Flow

```
PR diff ──► deterministic checks ──┐
        └─► LLM review ────────────┴─► merge + dedupe ──► policy ──► decision + PR comment + telemetry
```

Same code path in CI, locally (`--dry-run`) and in the eval harness.

## 2. Finding schema

| Field           | Type                              | Notes                                              |
|-----------------|-----------------------------------|----------------------------------------------------|
| `severity`      | `S1` \| `S2` \| `S3`              | As defined in TEAM-STANDARDS §6                    |
| `rule`          | string, section number, e.g. `"3"` | Section of TEAM-STANDARDS that is violated        |
| `file`          | repo-relative path                | Must be a file touched by the PR (or a helper it calls) |
| `line`          | int                               | Line in the head version of `file`                 |
| `explanation`   | string                            | Why it violates the rule; no PII values quoted     |
| `suggested_fix` | string                            | Concrete, minimal change                           |
| `source`        | `deterministic` \| `llm`          |                                                    |
| `confidence`    | float 0–1, optional               | LLM only; deterministic findings are implicitly 1.0 |

Invalid LLM output (fails schema validation) is retried; retries are counted in telemetry.
Findings pointing to files outside the PR's context are dropped.

## 3. Deterministic vs LLM

| Check                                                        | Owner         | Severity |
|--------------------------------------------------------------|---------------|----------|
| Secret patterns in added lines (§5)                          | deterministic | S1       |
| `ruff check` fails                                           | deterministic | S3       |
| `pytest` fails                                               | deterministic | S2       |
| Logic change in `app/` without changes in `tests/` (§2)      | deterministic | S2 candidate (confirmed by LLM) |
| PII attribute (`.full_name`, `.email`, `.birthdate`) passed directly as a log argument, not wrapped in `redact()` (§3) | deterministic | S1 |
| PII field names elsewhere in a log call (inside `redact()`, `extra`, helper calls) (§3) | deterministic | S1 candidate (confirmed by LLM) |
| New persisted/copied dataset without a `RETENTION_DAYS` entry (§4) | deterministic (heuristic) | S1 candidate |
| Indirect PII leaks: helpers, `extra`, object serialization (§3) | LLM        | S1       |
| Minors' data leaving the service without minimization / legal basis (§3, §4) | LLM | S1 |
| Ineffective tests: trivial asserts, mocked logic, no business case (§2) | LLM | S2 |
| Logic bugs that produce incorrect data (§6)                  | LLM           | S2       |
| Style, naming, refactoring (§1)                              | LLM           | S3       |

"Candidate" findings are passed to the LLM as hints; the LLM confirms or discards them.
A confirmed candidate has `source = llm` (the model made the final call) and goes through the
LLM rows of the policy. Telemetry counts candidates confirmed vs discarded.

## 4. LLM input

The model receives, in this order:
1. `TEAM-STANDARDS.md` in full.
2. Privacy context, always: source of `app/privacy.py` and `app/models.py`, plus a note on the
   known limits of `redact()`: it masks only top-level keys with exact names (`full_name`,
   `email`, `birthdate`); it does not inspect values (`{"contact": student.email}` leaks); it
   only accepts dicts; it returns a copy, so logging the original object still leaks.
3. The PR diff, plus the full head content of touched files and of helpers called from added
   lines (capped at a token budget; truncation is recorded in telemetry).
4. Deterministic candidates to confirm or discard.
5. The output JSON schema (§2).
6. Instruction: report only violations of explicit rules in TEAM-STANDARDS; do not report
   preferences or behavior the framework already guarantees; never quote PII values.

## 5. Merge and dedupe

Two findings are duplicates when they share `file`, `rule` and their lines are within ±3.
On conflict, keep the deterministic one and the higher severity.
The tolerance is tighter than eval matching (§7, ±5): dedupe merges two detections of the same
code; eval matching tolerates a human and a model choosing different lines for the same issue.

## 6. Decision policy (initial — to be tuned with eval metrics)

| Condition                                                        | Decision |
|------------------------------------------------------------------|----------|
| Any deterministic S1 or S2                                       | **block** |
| Any LLM S1 with `confidence ≥ 0.8`                               | **block** |
| Any other LLM S1/S2                                              | **review** (comment + request human review; check status red/pending) |
| Only S3 findings                                                 | **comment** |
| No findings                                                      | **approve** |

`confidence` is self-reported by the model and poorly calibrated; the 0.8 threshold is a starting
point to be validated in the eval. Agreement across repeated runs is a better signal (Plan B).

Rationale: TEAM-STANDARDS says S1/S2 block merges, but that rule is for humans. The bot only
auto-blocks categories whose precision is measured as high and stable (see `EVAL.md`).
If the LLM call fails after retries, the gate does not approve: it reports deterministic findings
and asks for human review.

## 7. Evaluation unit

Ground truth lives in `eval/ground_truth.yaml` (one entry per PR: `pr`, `expected_decision`,
`issues[]` with `id`, `severity`, `rule`, `file`, `line`, `ambiguous`, `also_at`).

- **Unit = finding.** A reviewer finding is a **true positive** when it matches a ground-truth
  issue with the same `rule` and a location within ±5 lines of `file:line` **or of any
  `also_at` location** (the same issue reported at another place it occurs).
- Each ground-truth issue matches at most one finding; extra duplicates of a matched issue are
  ignored, not counted as false positives.
- Issues marked `ambiguous: true` are excluded from recall (missing them is not a false negative),
  and a finding that matches one is neither TP nor FP. Metrics are also reported including them.
- Severity mismatch on a matched finding is a TP but is reported separately (severity accuracy).
- **Precision** = TP / reviewer findings; **Recall** = TP / ground-truth issues.
  Computed overall and per category (S1, S2, S3; deterministic vs LLM).
- **Decision accuracy**, two columns:
  - *Standards decision*: the gate's decision before the trust policy (any S1/S2 → block)
    vs `expected_decision`. Measures whether the reviewer found the right issues.
  - *Gate decision*: after the trust policy, collapsed to **stopped** (`block`, `review`) vs
    **let through** (`comment`, `approve`). `expected_decision: block` counts as correct if stopped.
    Measures whether the gate protects main.
- Each eval run appends to `eval_history.jsonl`: model, prompt version, metrics, cost, latency.

## 8. Privacy and telemetry

The reviewer follows the rules it enforces: telemetry logs metadata only (model, prompt version,
tokens, cost, latency, retries, finding counts, decision). No diffs, prompts or PII values in logs.
The API key comes from an environment variable only.

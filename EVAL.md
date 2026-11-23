# EVAL — AI Quality Gate

How well the reviewer does against a human-written ground truth, where it fails, and how much the
gate trusts it. Method: `SPEC.md` §7. Raw data: `eval/eval_history.jsonl` (one line per eval run)
and `eval/runs/<timestamp>.json` (every finding and its match).

## 1. Ground truth

`eval/ground_truth.yaml`, written by hand **before** the reviewer existed (see git history), for the
8 golden-set PRs in `PULL_REQUESTS.md`: 13 issues, one `expected_decision` per PR (7 `block`,
1 `approve`). Issues the author considered debatable are marked `ambiguous: true`; issues that occur
in more than one place list the extra locations in `also_at`.

> ✍️ **Author:** add a short per-PR summary here (problem, severity, rule), in your own words.

## 2. Results

Model `anthropic/claude-opus-5-5` through LiteLLM. "Strict" excludes ambiguous issues; "inclusive"
counts them as real. Decision accuracy has two columns (SPEC §7): *standards* = what TEAM-STANDARDS
would decide on the findings; *gate* = the gate's decision after the trust policy, collapsed to
stopped (`block`/`review`) vs let through (`comment`/`approve`).

| Run | Prompt | Runs | Precision | Recall | Gate acc. | Standards acc. | Cost/PR | Latency/PR |
|-----|--------|------|-----------|--------|-----------|----------------|---------|------------|
| Deterministic only¹ | — | 1 | 0.455 | 0.500 | 0.625 | 0.625 | $0 | <1 s |
| First LLM run | `d96f0f` | 1 | 0.769 | 1.000 | 1.000 | 1.000 | $0.034 | 8.6 s |
| Stability | `d96f0f` | 3 | 0.690 (0.643–0.714) | 0.967 (0.9–1.0) | 0.958 (0.875–1.0) | 0.917 | $0.036 | 9.5 s |
| Iteration: severity heuristic | `7d4d76` | 3 | 0.698 (0.667–0.714) | 1.000 | 0.917 (0.875–1.0) | 0.917 | $0.036 | 9.3 s |
| **Final: severity restates §6** | `7a9ad9` | 1 | **0.714** | **1.000** | **1.000** | **1.000** | $0.035 | 8.8 s |

¹ `--no-llm`: unconfirmed candidates count as findings in this mode, which lowers precision. An
earlier baseline in the history (P 0.455, R 0.455) predates the last ground-truth edit.

**Final run, detail** (strict): 10 TP, 4 FP, 0 FN; severity accuracy 1.0. Inclusive: precision 0.733,
recall 0.846. By source: deterministic 3/3 precise, LLM 7/11. By severity: S1 6 TP / 1 FP,
S2 3 TP / 3 FP, S3 1 TP / 0 FP.

**Decision stability** (3 runs, prompt `d96f0f`): 6 of 8 PRs got the same decision every time.
`feature/score-validation` went review, review, **comment** (let through once);
`feature/lessons-pagination` went approve, approve, comment (let through every time, acceptable).

Total eval spend so far: about $2.30 for 8 LLM runs of the golden set.

## 3. Failure analysis

### False negative — a found issue is downgraded and the PR gets through

- **What:** in `feature/score-validation` the reviewer found the input-validation problem at
  `app/main.py:52` in all 3 stability runs, but rated it S2, S2 and **S3**. With S3 the decision was
  `comment` and the PR was let through.
- **Why:** finding-level recall hid it (the finding matched, so it counted as a TP); the failure is
  in *classification*, and only decision-level metrics show it. The prompt left the S2/S3 boundary
  to the model.
- **What we changed:** iteration 1 added "a violation of an explicit rule is at least S2". It fixed
  this PR (review 3/3) but pushed a minor test gap in `feature/lessons-pagination` to S2, so the gate
  asked for review on a PR that should be approved (2/3). Fixing one case broke another: a sign of
  tuning against the golden set. The final prompt restates §6 only, with no examples taken from the
  golden set, and was measured once (see §5).
- **Related:** in the final run the same finding was reported as rule §6 (truncating 99.9 to 99
  stores incorrect data) while the ground truth has it as §1 (unvalidated input). Detection and
  severity were right; the eval's exact `rule` match counted it as FP + FN. The issue genuinely fits
  both categories. **Next change:** let a ground-truth issue accept more than one rule.

### False positive — logs treated as a dataset without retention

- **What:** in `feature/support-context` the reviewer reported, in 3/3 runs, an S1 §4 finding: the
  support log is a secondary copy of personal data (including minors') without a retention category.
  Two other false positives are just as stable: "the new helper has no tests" (§2) in
  `feature/support-context` and in `feature/analytics-archive`.
- **Why:** they are consistent readings of TEAM-STANDARDS, not noise: §4 forbids "secondary copies
  of personal data without a defined purpose and retention period", and §2 asks for tests on every
  logic change. The ground truth reports only the main §3 issue in these PRs.
- **What to change:**
  > ✍️ **Author:** decide per finding: (a) the reviewer over-reports → clarify the standard (for
  > example "logs are governed by §3; §4 applies to stored datasets"), which fixes it for every PR;
  > or (b) the issue is real → add it to the ground truth. Do not patch the prompt for this case.

## 4. Trust policy, justified

Precision per source and severity, pooled over all 8 LLM runs of the golden set (a finding counts
as precise when it matches a ground-truth issue, ambiguous ones included):

| Source / severity | Precise / total | Precision | Policy |
|-------------------|-----------------|-----------|--------|
| Deterministic S1 (not candidate) | 24 / 24 | 1.00 | **block** |
| LLM S1, confidence ≥ 0.8 | 24 / 27 | 0.89 | **block** |
| LLM S1, confidence < 0.8 | 0 / 5 | 0.00 | review |
| LLM S2 (all) | 41 / 65 | 0.63 | review |
| LLM S3 | 8 / 9 | 0.89 | comment (never blocks) |

- Deterministic findings were never wrong, so they block on their own.
- The 0.8 threshold separates LLM S1 findings sharply: above it they are right 9 times out of 10,
  below it never. Self-reported confidence is poorly calibrated in general (ADR-003); here it is
  measured to work, on a small set.
- LLM S2 is right about 6 times out of 10, which is too low to block a merge automatically. It asks
  for a human instead. This is why 3 PRs end in `review` rather than `block`, and why the gate still
  stops 8/8 PRs that should be stopped.
- Review is cheap and wrong blocks erode trust in the gate, so the policy prefers `review` when
  unsure. Override rate in production (humans merging a `review`/`block`) is the live signal to
  revisit these thresholds.

## 5. Limitations

- **Small set, same set for tuning and testing.** 8 PRs and 13 issues; a difference of one PR is
  within run-to-run variance. Prompt changes after the first iteration were chosen by principle, not
  by score (ADR-006). Generalization is measured on new PRs the reviewer was never tuned on (step 9).
- **Exact rule matching** penalizes issues that fit two categories (see §3).
- **Self-reported confidence** drives the S1 threshold; agreement across repeated runs is the better
  signal if this set grows.
- **Single model.** Comparing a cheaper model is pending (Plan B).

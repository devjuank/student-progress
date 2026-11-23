# AI usage

## Tools

- **Claude Code** (Claude Opus 5.5) in the terminal: security check of the challenge repo, repo
  setup (Python 3.12 venv, private GitHub repo, opening the 8 PRs), implementation, tests, eval runs
  (about $2.30 of API usage) and doc drafts.
- **Claude** (chat): planning the challenge, explaining concepts, a second-opinion review of
  `SPEC.md` before implementation, help wording parts of the ground truth and the docs, and
  rehearsing the presentation.
- **The reviewer itself** calls `claude-opus-5-5` through LiteLLM.

## What stayed human

- **Ground truth classification** (`eval/ground_truth.yaml`): which issues exist, their severity,
  rule and expected decision were my judgment, written before the reviewer existed. Claude Code
  never edited it. While building the reviewer it only inspected key names and value formats
  (allowed severities, rule numbers, line and `also_at` shapes) to align the eval matcher, not the
  issue content. From the first eval run on, the eval output showed it which ground-truth ids were
  matched or missed, and I described part of my classification in chat; that is the information the
  step-7 iteration used. It read my per-PR summary in `EVAL.md` only after the final prompt was fixed.
- **What Claude Code saw before the ground truth (disclosed):** it read all 8 diffs during the
  initial security check, before I wrote the ground truth. It did not list the issues, to keep the
  ground truth mine, except for warning that one branch contained a string shaped like an API key
  (so the repo had to be private).
- **Where AI helped with the ground truth (disclosed):** I used Claude (chat) to explain concepts
  (FastAPI type validation, the limits of `redact()`, `round` vs `int`), as a second opinion on one
  PR (lessons-pagination) after reviewing it myself, and to help word the descriptions of several
  issues. The classification was mine; the wording was assisted.
- **One prompt rule influenced by the ground truth:** "when a logic bug also lacks tests, report the
  bug and the tests separately" was added after I described my classification to Claude Code. It is
  a general rule (two distinct sections in TEAM-STANDARDS), but it may make metrics slightly
  optimistic.
- **Decisions:** severity criteria, rule taxonomy, trust-policy thresholds, leaving the ground truth
  unchanged after seeing results, and when to stop iterating.

## Most important prompts

1. **Security check of the challenge zip** before running anything: "check these files for malware or
   vulnerabilities; this is a job-interview exercise with a small service and 8 PRs".
2. **SPEC review with a second model**: six observations (PII check false positives on correct
   `redact()` use, which `source` a confirmed candidate gets, decision accuracy being wrong by
   design, missing "LLM input" section, dedupe vs matching tolerance, self-reported confidence).
   I asked Claude Code to evaluate each one and propose edits without changing anything until I
   approved. All six were applied.
3. **"Everything we do should be aligned to the SPEC"**: from then on every implementation detail
   (the `candidate` field, line anchoring, the `redact()` exact-key rule, the `review` decision) was
   reflected back into `SPEC.md` before committing.
4. **Rule taxonomy as a general rule**: "1 input/code quality, 2 tests, 3 PII, 4 retention,
   5 secrets, 6 logic bugs — in the prompt as a general rule, not a hint for any PR".
5. **Overfitting check**: after a severity rule fixed one case and broke another, I asked "aren't we
   overfitting to the PRs we have?" → the final wording restates §6 only and was measured once
   (ADR-006).

## AI output discarded or corrected

- **Prompt examples drawn from the golden set (discarded).** The first LLM prompt listed test smells
  and edge cases (`>= 0` assertions, type-only checks, rounding, tests encoding a wrong expectation)
  that Claude Code had seen in the PR diffs. Caught by Claude Code itself when re-reading its prompt:
  that is overfitting to the golden set. They were removed and only TEAM-STANDARDS criteria kept.
- **Severity heuristic (rolled back).** "A violation of an explicit rule is at least S2" fixed a
  false negative and created a false positive. The follow-up wording Claude Code proposed was written
  knowing a ground-truth label; it was proposed but not applied. Replaced by a restatement of §6
  (EVAL.md §3, ADR-006).
- **Finding line anchoring (corrected).** The "logic without tests" check pointed at module
  docstrings and imports; with the eval's ±5-line matching that would have counted one real issue as
  a false positive and a false negative. It now points at the first line of real code.
- **Data errors in EVAL.md (corrected on review).** Checking the doc against the eval output found
  four figures that did not match the data ("5 of 8 PRs with S1", "8/8 PRs stopped", "precision would
  rise to 0.83", "only the main §3 issue"); two of them came from Claude Code's draft. Every number in
  EVAL.md is now traceable to `eval/runs/`.
- **Fresh-clone test (bug found).** Testing the README from a fresh clone found that the reviewer
  failed when only `origin/main` existed locally; it now resolves `origin/<ref>`.
- **Co-author trailer on the ground-truth commit (removed).** Claude Code committed the human ground
  truth with a `Co-Authored-By: Claude` line, contradicting the point of the commit; it was undone and
  committed by hand.
- **Plan assumption corrected: low temperature.** The plan asked for low temperature; current Claude
  models reject `temperature`, and LiteLLM's `response_format`/`reasoning_effort` can map to rejected
  request shapes. Replaced by prompt-level JSON with schema validation and retries (ADR-004).
- **CI hardening (added on review).** While designing the workflow, Claude Code noticed a PR could
  edit `TEAM-STANDARDS.md` or `PII_FIELDS` to weaken its own review; standards are now read from the
  base branch and PII fields are the union of base and head (ADR-005).

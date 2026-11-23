# AI usage

> ✍️ **Draft prepared from the session log.** Rewrite it in your own words: the prompts, judgments
> and corrections below are yours to tell. Remove this note when done.

## Tools

- **Claude Code** (Claude Opus 5.5) in the terminal: repository security check, implementation,
  tests, eval runs and doc drafts.
- **Claude** (chat): second-opinion review of `SPEC.md` before implementation.
- **The reviewer itself** calls `claude-opus-5-5` through LiteLLM.

## What stayed human

- **Ground truth** (`eval/ground_truth.yaml`): written by hand before the reviewer existed and never
  read or edited by the AI. The AI only inspected key names and value formats to align the eval
  matcher, without seeing issue content.
- Decisions on severity criteria, rule taxonomy, trust policy thresholds and when to stop iterating.

## Most important prompts

1. **Security check of the challenge zip** before running anything: "check these files for malware or
   vulnerabilities; this is a job-interview exercise with a small service and 8 PRs".
2. **SPEC review with a second model**: six observations (PII check false positives on correct
   `redact()` use, which `source` a confirmed candidate gets, decision accuracy being wrong by
   design, missing "LLM input" section, dedupe vs matching tolerance, self-reported confidence).
   All six were applied.
3. **Rule taxonomy as a general rule**: "1 input/code quality, 2 tests, 3 PII, 4 retention,
   5 secrets, 6 logic bugs — in the prompt as a general rule, not a hint for any PR".
4. **Severity guidance** and, after it broke another case, "aren't we overfitting to the PRs we
   have?" → final wording restates §6 and is measured once (ADR-006).

## AI output discarded or corrected

- **Prompt examples drawn from the golden set (discarded).** The first LLM prompt listed test smells
  and edge cases (`>= 0` assertions, type-only checks, rounding, tests encoding a wrong expectation)
  that the AI had seen in the PR diffs. That is overfitting to the golden set; they were removed and
  only TEAM-STANDARDS criteria kept.
- **Severity heuristic (rolled back).** "A violation of an explicit rule is at least S2" fixed a
  false negative and created a false positive; the follow-up wording was written knowing a
  ground-truth label. Replaced by a restatement of §6 (EVAL.md §3, ADR-006).
- **Finding line anchoring (corrected).** The "logic without tests" check pointed at module
  docstrings and imports; with the eval's ±5-line matching that would have counted one real issue as
  a false positive and a false negative. It now points at the first line of real code.
- **Co-author trailer on the ground-truth commit (removed).** The AI committed the human ground truth
  with a `Co-Authored-By: Claude` line, contradicting the point of the commit; it was undone and
  committed by hand.
- **API assumptions (corrected).** The plan asked for low temperature; current Claude models reject
  `temperature`, and LiteLLM's `response_format`/`reasoning_effort` can map to rejected request
  shapes. Replaced by prompt-level JSON with schema validation and retries (ADR-004).
- **CI hardening (added on review).** While designing the workflow, the AI noticed a PR could edit
  `TEAM-STANDARDS.md` or `PII_FIELDS` to weaken its own review; standards are now read from the base
  branch and PII fields are the union of base and head (ADR-005).

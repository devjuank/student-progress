# student-progress + AI Quality Gate

Open English LMS service that tracks students' lesson progress, plus an **AI Quality Gate** that
reviews every pull request against `TEAM-STANDARDS.md` and decides to **block**, ask for **review**,
**comment** or **approve**.
**Some students are minors** — read `TEAM-STANDARDS.md` before making any changes.

| Document | What it covers |
|----------|----------------|
| `SPEC.md` | Reviewer contract: finding schema, deterministic vs LLM checks, policy, eval method |
| `EVAL.md` | Results against the human ground truth, failure analysis, trust policy |
| `DECISIONS.md` | Architecture decisions (ADRs) and what was left out |
| `AI-USAGE.md` | How AI tools were used to build this |

## Quick start (≈10 minutes)

Requires **Python 3.11+** (CI uses 3.12) and git. An Anthropic API key is only needed for the LLM
review; everything else runs without one.

```bash
git clone <this repo> && cd student-progress
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # ~1–2 min
pytest                                     # service + reviewer tests, no API calls

# Make the golden-set PR branches available locally (skip if you already have them):
git fetch origin '+refs/heads/*:refs/remotes/origin/*'
for b in $(git branch -r | grep -v -e HEAD -e main); do git branch --track "${b#origin/}" "$b" 2>/dev/null; done
```

### Review one PR locally

```bash
# Deterministic checks only (free, no key):
python -m reviewer --head feature/email-reminders --no-llm

# Full review with the LLM:
cp .env.example .env                       # then paste your ANTHROPIC_API_KEY into .env
python -m reviewer --head feature/email-reminders --report report.md
```

Prints the result as JSON (decision, findings, model, cost) and, with `--report`, writes the
Markdown comment the gate posts on the PR. Exit code is 1 when the decision is `block`. Nothing is
posted to GitHub from a local run.

### Run the eval

```bash
python -m reviewer.eval --no-llm                         # deterministic baseline, free
python -m reviewer.eval --note "what changed"            # 8 PRs, ~$0.30, ~1.5 min
python -m reviewer.eval --runs 3 --note "stability"      # decision stability, ~$0.90
```

Prints a per-PR table (expected vs gate decision, TP/FP/FN) and precision, recall, decision
accuracy, cost and latency; saves every finding to `eval/runs/` and appends a line to
`eval/eval_history.jsonl`. The ground truth is `eval/ground_truth.yaml`.

### Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | — | API key (from `.env` locally, GitHub secret in CI). Never commit it. |
| `REVIEWER_MODEL` | `anthropic/claude-opus-5-5` | Any LiteLLM model string, e.g. `anthropic/claude-haiku-5-5` |
| `REVIEWER_TELEMETRY_FILE` | `telemetry.jsonl` | Run metadata log (no diffs, prompts or PII) |

## CI

`.github/workflows/quality-gate.yml` runs on every pull request in two jobs: `tools` runs ruff +
pytest on the PR's code **without secrets**; `review` runs the static checks and the LLM review
**with the API key**, using the reviewer from the base branch and never executing the PR's code
(ADR-005). It posts (and on reruns, updates) a PR comment and a job summary; `block` fails the check,
`review` passes with a warning.

Setup, once:

```bash
gh secret set ANTHROPIC_API_KEY             # paste the key when prompted
gh variable set REVIEWER_MODEL --body anthropic/claude-opus-5-5   # optional
```

**Live demo:** PR #9 (`rehearsal/leaderboard`, a PR the reviewer was never tuned on) shows the gate
on a real `pull_request` event: the `AI review` check is red and the comment lists the findings
(EVAL.md §6). PRs #1–#8 are the golden set; their gate comments come from manual reruns.

The reviewer must be on the base branch (`main`) to review PRs against it. To re-run the gate on an
existing PR: `gh workflow run quality-gate -f pr=<number>`.

## The service

```bash
uvicorn app.main:app --reload
```

- `GET /health`
- `GET /lessons`
- `GET /students/{id}/progress`
- `POST /students/{id}/progress` — body: `{"lesson_id": "...", "score": 0-100}`

Data is held in memory (see `app/store.py`). There is no database: the focus of this repository is
the review process, not persistence.

## Layout

```
app/                     the service under review
reviewer/
  cli.py                 entry point: python -m reviewer
  diff.py                reads a PR from git (merge base vs head)
  checks.py              deterministic checks: secrets, PII in logs, retention, tests, ruff, pytest
  llm.py                 LLM review through LiteLLM (prompt, validation, retries, cost)
  policy.py              merge/dedupe and block/review/comment/approve decision
  report.py              PR comment / job summary
  telemetry.py           metadata-only JSONL per run
  eval.py                eval harness: python -m reviewer.eval
eval/                    ground truth, eval runs and history
tests/reviewer/          reviewer tests (synthetic PRs, fake LLM)
```

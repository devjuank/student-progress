"""LLM review through LiteLLM (SPEC.md §4). Model comes from REVIEWER_MODEL."""
import ast
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from reviewer.checks import MODELS_MODULE, PRIVACY_MODULE
from reviewer.diff import PRContext
from reviewer.schema import Finding, validate

DEFAULT_MODEL = "anthropic/claude-opus-5-5"
MAX_TOKENS = 16000
MAX_ATTEMPTS = 3
CONTEXT_CHAR_BUDGET = 200_000
STANDARDS_FILE = "TEAM-STANDARDS.md"

SYSTEM_PROMPT = """\
You are the automated code reviewer of a pull request. You review it strictly against the team
standards given below and report violations as JSON.

How to review:
- Report only violations of explicit rules in TEAM-STANDARDS. Do not report preferences, style the
  rules do not mention, or behavior the framework already guarantees.
- Follow data, not names: trace where personal data flows through helpers, dicts, `extra` fields and
  object serialization (dataclass repr, f-strings, dict copies). A log or copy that reaches PII
  through an indirect path is as much a leak as a direct one.
- Use the redact() notes: it only masks top-level keys with exact PII names, does not inspect
  values, only accepts dicts, and returns a copy.
- Data of minors (`is_minor`) is `pii-minor`. Copying it outside the service's core purpose
  (analytics, support tooling, third parties) without minimization, retention and a documented legal
  basis is S1.
- For tests, ask whether they would fail if the behavior broke (TEAM-STANDARDS §2).
- For logic, check edge cases and boundary conditions against what the PR description and the code
  intend.
- Severity follows TEAM-STANDARDS §6. `rule` is the number of the TEAM-STANDARDS section whose rule
  is violated ("1".."5"; use "6" only if no other section applies).
- `line` is the line in the head version of the file where a reviewer would mark the issue (the
  offending statement, not the module docstring or an import).
- Never quote PII values or secret values in explanations.
- Deterministic candidates are hints from static checks. Confirm one by reporting it as your own
  finding (same file and rule) if it is a real violation; otherwise leave it out.
- If the PR has no violations, return an empty list. Do not invent issues to have something to say.

Respond with only a JSON object, no prose and no code fences:
{"findings": [{"severity": "S1|S2|S3", "rule": "<section number>", "file": "<path>", "line": <int>,
"explanation": "<why it violates the rule>", "suggested_fix": "<minimal concrete change>",
"confidence": <0.0-1.0>}]}
"""

REDACT_NOTES = """\
Known limits of app.privacy.redact(): it masks only top-level keys whose name is exactly one of
PII_FIELDS; it does not inspect values (`{"contact": student.email}` leaks); it only accepts dicts;
it returns a copy, so logging the original object still leaks."""

PROMPT_VERSION = hashlib.sha256((SYSTEM_PROMPT + REDACT_NOTES).encode()).hexdigest()[:12]


class LLMReviewError(Exception):
    """The LLM did not return valid findings after all attempts."""


@dataclass
class LLMResult:
    findings: list[Finding]
    model: str
    prompt_version: str = PROMPT_VERSION
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0
    attempts: int = 0
    dropped_findings: int = 0
    truncated_context: bool = False
    candidates_sent: int = 0
    stats: dict = field(default_factory=dict)


def _imported_app_modules(source: str) -> set[str]:
    """Repo paths of app modules imported by a file (helpers its code may call)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
            modules.add(node.module)
            modules |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Import):
            modules |= {alias.name for alias in node.names if alias.name.startswith("app")}
    return {m.replace(".", "/") + ".py" for m in modules}


def build_context_files(ctx: PRContext) -> tuple[dict[str, str], bool]:
    """Head content of touched files, privacy/model modules and helpers, within a char budget."""
    touched = [p for p, c in ctx.files.items() if c.status != "D"]
    wanted = [*touched, PRIVACY_MODULE, MODELS_MODULE]
    for path in touched:
        if path.endswith(".py"):
            wanted += sorted(_imported_app_modules(ctx.head(path) or ""))
    files, used, truncated = {}, 0, False
    for path in dict.fromkeys(wanted):
        content = ctx.head(path)
        if content is None:
            continue
        if used + len(content) > CONTEXT_CHAR_BUDGET:
            truncated = True
            continue
        files[path] = content
        used += len(content)
    return files, truncated


def build_messages(
    ctx: PRContext, diff_text: str, standards: str, candidates: list[Finding], pr_description: str = ""
) -> tuple[list[dict], set[str], bool]:
    files, truncated = build_context_files(ctx)
    numbered = {
        path: "\n".join(f"{n:4d}  {line}" for n, line in enumerate(content.splitlines(), 1))
        for path, content in files.items()
    }
    candidate_rows = [
        {"severity": c.severity, "rule": c.rule, "file": c.file, "line": c.line, "hint": c.explanation}
        for c in candidates
    ]
    user = "\n\n".join([
        f"<team_standards>\n{standards}\n</team_standards>",
        f"<redact_notes>\n{REDACT_NOTES}\n</redact_notes>",
        f"<pr_description>\n{pr_description or '(none)'}\n</pr_description>",
        f"<diff>\n{diff_text}\n</diff>",
        *(f'<file path="{p}" version="head">\n{body}\n</file>' for p, body in numbered.items()),
        f"<deterministic_candidates>\n{json.dumps(candidate_rows, indent=1)}\n</deterministic_candidates>",
    ])
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
    return messages, set(files), truncated


def parse_findings(text: str, known_files: set[str]) -> tuple[list[Finding], int]:
    """Parses the model's JSON. Raises ValueError if the envelope is invalid.

    Individual findings that fail the schema or point outside the PR's context are dropped and counted.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in response")
    data = json.loads(match.group(0))
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        raise ValueError('response must be an object with a "findings" list')
    findings, dropped = [], 0
    for raw in data["findings"]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        item = {**raw, "rule": str(raw.get("rule")), "source": "llm", "candidate": False}
        if validate(item) or item["file"] not in known_files:
            dropped += 1
            continue
        findings.append(Finding(**{k: item[k] for k in Finding.__dataclass_fields__ if k in item}))
    return findings, dropped


def _default_completion(**kwargs):
    import litellm  # imported lazily: deterministic-only runs do not need it

    return litellm.completion(**kwargs)


def _cost(response) -> float | None:
    try:
        import litellm

        return litellm.completion_cost(completion_response=response)
    except Exception:  # unknown model pricing must not fail the review
        return None


def review(
    ctx: PRContext,
    diff_text: str,
    candidates: list[Finding],
    pr_description: str = "",
    repo: str = ".",
    completion: Callable = _default_completion,
) -> LLMResult:
    model = os.environ.get("REVIEWER_MODEL", DEFAULT_MODEL)
    # Standards come from the base: a PR must not be able to rewrite the rules it is judged by.
    standards = ctx.base(STANDARDS_FILE) or Path(repo, STANDARDS_FILE).read_text()
    messages, known_files, truncated = build_messages(ctx, diff_text, standards, candidates, pr_description)
    result = LLMResult(findings=[], model=model, truncated_context=truncated, candidates_sent=len(candidates))
    start = time.monotonic()
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        result.attempts = attempt
        response = completion(model=model, messages=messages, max_tokens=MAX_TOKENS)
        usage = getattr(response, "usage", None)
        result.input_tokens += getattr(usage, "prompt_tokens", 0) or 0
        result.output_tokens += getattr(usage, "completion_tokens", 0) or 0
        cost = _cost(response)
        if cost is not None:
            result.cost_usd = (result.cost_usd or 0) + cost
        text = response.choices[0].message.content or ""
        try:
            result.findings, result.dropped_findings = parse_findings(text, known_files)
            break
        except ValueError as error:  # json.JSONDecodeError is a ValueError
            last_error = str(error)
            messages = [*messages, {"role": "assistant", "content": text}, {
                "role": "user",
                "content": f"Your response was not valid ({last_error}). Reply with only the JSON object.",
            }]
    else:
        result.latency_s = time.monotonic() - start
        raise LLMReviewError(f"invalid response after {MAX_ATTEMPTS} attempts: {last_error}")
    result.latency_s = time.monotonic() - start
    return result


def result_metadata(result: LLMResult) -> dict:
    """Telemetry-safe view of an LLM result: metadata only, no findings text."""
    data = asdict(result)
    data.pop("findings")
    data["llm_findings"] = len(result.findings)
    return data

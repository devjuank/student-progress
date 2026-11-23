"""LLM response handling and decision policy, with a fake completion (no API calls)."""
import json
from types import SimpleNamespace

import pytest

from reviewer import llm
from reviewer.diff import FileChange, PRContext
from reviewer.policy import decide, merge, standards_decision
from reviewer.schema import Finding

SOURCES = {
    "app/svc.py": "from app.helpers import fmt\n\n\ndef f(c):\n    return fmt(c)\n",
    "app/helpers.py": "def fmt(c):\n    return str(c)\n",
    "app/privacy.py": 'PII_FIELDS = {"email"}\n',
    "app/models.py": "class Customer:\n    email: str\n",
    "app/unrelated.py": "X = 1\n",
}


def make_ctx() -> PRContext:
    files = {"app/svc.py": FileChange("app/svc.py", "A", {1: "", 4: "def f(c):", 5: "    return fmt(c)"})}
    return PRContext(files, lambda ref, path: SOURCES.get(path), "base", "head")


def fake_response(text: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
    )


def llm_finding(**overrides) -> dict:
    return {"severity": "S1", "rule": "3", "file": "app/svc.py", "line": 5,
            "explanation": "leak", "suggested_fix": "redact", "confidence": 0.9, **overrides}


def det(severity="S1", rule="3", file="app/svc.py", line=5, candidate=False) -> Finding:
    return Finding(severity, rule, file, line, "x", "y", "deterministic", candidate=candidate)


def llm_f(severity="S1", rule="3", file="app/svc.py", line=5, confidence=0.9) -> Finding:
    return Finding(severity, rule, file, line, "x", "y", "llm", confidence=confidence)


# --- context ---


def test_context_includes_touched_files_privacy_models_and_imported_helpers():
    files, truncated = llm.build_context_files(make_ctx())
    assert set(files) == {"app/svc.py", "app/helpers.py", "app/privacy.py", "app/models.py"}
    assert not truncated


def test_context_budget_marks_truncation(monkeypatch):
    monkeypatch.setattr(llm, "CONTEXT_CHAR_BUDGET", 60)
    files, truncated = llm.build_context_files(make_ctx())
    assert truncated and sum(map(len, files.values())) <= 60


# --- parsing ---


def test_parse_findings_sets_source_and_drops_invalid_or_unknown_files():
    text = "```json\n" + json.dumps({"findings": [
        llm_finding(),
        llm_finding(file="app/elsewhere.py"),
        llm_finding(severity="S7"),
        llm_finding(rule=3),
    ]}) + "\n```"
    findings, dropped = llm.parse_findings(text, {"app/svc.py"})
    assert [(f.source, f.rule, f.candidate) for f in findings] == [("llm", "3", False), ("llm", "3", False)]
    assert dropped == 2


@pytest.mark.parametrize("text", ["no json here", '{"other": []}', "{not json}"])
def test_parse_findings_rejects_invalid_envelope(text):
    with pytest.raises(ValueError):
        llm.parse_findings(text, {"app/svc.py"})


# --- review loop ---


def test_review_retries_invalid_json_and_accumulates_usage(tmp_path, monkeypatch):
    (tmp_path / "TEAM-STANDARDS.md").write_text("rules")
    monkeypatch.setenv("REVIEWER_MODEL", "test/model")
    replies = iter(["sorry, here it is", json.dumps({"findings": [llm_finding()]})])
    calls = []

    def completion(**kwargs):
        calls.append(kwargs)
        return fake_response(next(replies))

    result = llm.review(make_ctx(), "diff", [], repo=str(tmp_path), completion=completion)
    assert result.attempts == 2
    assert (result.input_tokens, result.output_tokens) == (200, 40)
    assert len(result.findings) == 1 and result.model == "test/model"
    assert "temperature" not in calls[0]  # rejected by current Claude models
    assert calls[1]["messages"][-1]["role"] == "user"


def test_review_raises_after_max_attempts(tmp_path):
    (tmp_path / "TEAM-STANDARDS.md").write_text("rules")
    with pytest.raises(llm.LLMReviewError):
        llm.review(make_ctx(), "diff", [], repo=str(tmp_path), completion=lambda **_: fake_response("nope"))


def test_prompt_contains_standards_candidates_and_numbered_files():
    candidate = det(candidate=True)
    messages, _, _ = llm.build_messages(make_ctx(), "DIFF", "STANDARDS", [candidate], "why")
    user = messages[1]["content"]
    assert "STANDARDS" in user and "DIFF" in user and '"line": 5' in user
    assert "   5      return fmt(c)" in user


def test_metadata_has_no_finding_text():
    result = llm.LLMResult(findings=[llm_f()], model="m")
    meta = llm.result_metadata(result)
    assert "findings" not in meta and meta["llm_findings"] == 1


# --- merge and policy ---


def test_merge_drops_candidates_and_dedupes_keeping_deterministic_and_higher_severity():
    deterministic = [det(severity="S2", line=10), det(rule="2", candidate=True)]
    merged = merge(deterministic, [llm_f(severity="S1", line=12), llm_f(rule="4", line=30)])
    assert [(f.source, f.severity, f.rule, f.line) for f in merged] == [
        ("deterministic", "S1", "3", 10),
        ("llm", "S1", "4", 30),
    ]


def test_merge_keeps_findings_beyond_dedupe_tolerance():
    assert len(merge([det(line=10)], [llm_f(line=14)])) == 2


@pytest.mark.parametrize(("findings", "llm_failed", "expected"), [
    ([det()], False, "block"),
    ([det(severity="S2", rule="2")], False, "block"),
    ([llm_f(confidence=0.8)], False, "block"),
    ([llm_f(confidence=0.5)], False, "review"),
    ([llm_f(severity="S2", confidence=0.99)], False, "review"),
    ([det(candidate=True)], False, "review"),
    ([llm_f(severity="S3")], False, "comment"),
    ([], False, "approve"),
    ([], True, "review"),
])
def test_decide(findings, llm_failed, expected):
    assert decide(findings, llm_failed) == expected


def test_standards_decision_ignores_trust_policy():
    assert standards_decision([llm_f(severity="S2", confidence=0.1)]) == "block"
    assert standards_decision([llm_f(severity="S3")]) == "comment"
    assert standards_decision([]) == "approve"

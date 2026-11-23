"""Deterministic checks against synthetic PRs (not the golden-set branches)."""
import pytest

from reviewer.checks import (
    check_logic_without_tests,
    check_pii_in_logs,
    check_retention,
    check_secrets,
    parse_junit,
    parse_ruff_output,
)
from reviewer.diff import FileChange, PRContext, parse_unified_diff
from reviewer.schema import Finding

PRIVACY = 'PII_FIELDS = {"full_name", "email", "birthdate"}\nRETENTION_DAYS = {"progress": 365}\n'
MODELS = (
    "from dataclasses import dataclass\n\n"
    "@dataclass\nclass Customer:\n    id: str\n    email: str\n\n"
    "@dataclass\nclass Course:\n    id: str\n    title: str\n"
)


def make_ctx(head_files: dict[str, str], base_files: dict[str, str] | None = None, added_all=True) -> PRContext:
    """PR where every line of each head file counts as added (or only the lines not in base)."""
    base_files = base_files or {}
    base = {"app/privacy.py": PRIVACY, "app/models.py": MODELS, **base_files}
    head = {**base, **head_files}
    files = {}
    for path, source in head_files.items():
        base_lines = set(base_files.get(path, "").splitlines()) if not added_all else set()
        added = {n: text for n, text in enumerate(source.splitlines(), 1) if text not in base_lines}
        files[path] = FileChange(path, "M" if path in base_files else "A", added)
    return PRContext(files, lambda ref, path: (head if ref == "head" else base).get(path), "base", "head")


# --- diff parsing ---


def test_parse_unified_diff_tracks_head_line_numbers():
    diff = (
        "diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n"
        "@@ -3,0 +4,2 @@\n+one\n+two\n@@ -10 +12 @@\n-old\n+new\n"
        "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    )
    assert parse_unified_diff(diff) == {"app/x.py": {4: "one", 5: "two", 12: "new"}}


# --- schema ---


def test_finding_rejects_invalid_fields():
    with pytest.raises(ValueError, match="severity"):
        Finding("S9", "3", "a.py", 1, "x", "y", "llm")
    with pytest.raises(ValueError, match="line"):
        Finding("S1", "3", "a.py", 0, "x", "y", "llm")


# --- §5 secrets ---


@pytest.mark.parametrize("line", [
    'API_KEY = "SG.abcdefghijklmnopqrstuv.ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"',
    'client = Client(token="ghp_abcdefghijklmnopqrstuvwxyz0123456789")',
    'db_password = "s3cr3t-very-long-value"',
    "aws = 'AKIAABCDEFGHIJKLMNOP'",
])
def test_secrets_detected_without_quoting_value(line):
    findings = check_secrets(make_ctx({"app/config.py": f"x = 1\n{line}\n"}))
    assert [(f.severity, f.rule, f.line) for f in findings] == [("S1", "5", 2)]
    assert "abcdefghij" not in findings[0].explanation


@pytest.mark.parametrize("line", [
    'API_KEY = os.environ["API_KEY"]',
    'API_KEY = "your-api-key-goes-here-123"',
    'token_ttl = "3600"',
])
def test_secrets_ignores_env_lookups_and_placeholders(line):
    assert check_secrets(make_ctx({"app/config.py": line})) == []


# --- §2 logic without tests ---


def test_logic_change_without_tests_is_candidate():
    findings = check_logic_without_tests(make_ctx({"app/x.py": "# comment\n\ndef f():\n    return 1\n"}))
    assert [(f.severity, f.rule, f.line, f.candidate) for f in findings] == [("S2", "2", 3, True)]


def test_logic_change_with_tests_is_fine():
    ctx = make_ctx({"app/x.py": "def f():\n    return 1\n", "tests/test_x.py": "def test_f():\n    pass\n"})
    assert check_logic_without_tests(ctx) == []


def test_comment_only_change_is_fine():
    assert check_logic_without_tests(make_ctx({"app/x.py": "# just a comment\n"})) == []


def test_logic_line_skips_docstring_and_imports_and_prefers_functions():
    code = '"""Module doc.\n\nMore.\n"""\nimport os\n\nCACHE = {}\n\n\ndef f():\n    """Doc."""\n    return os.sep\n'
    findings = check_logic_without_tests(make_ctx({"app/x.py": code}))
    assert [f.line for f in findings] == [10]


def test_logic_line_points_to_changed_statement_not_new_import():
    base = "import os\n\n\ndef f():\n    return 1\n"
    head = "import os\nimport sys\n\n\ndef f():\n    return sys.maxsize\n"
    ctx = make_ctx({"app/x.py": head}, base_files={"app/x.py": base}, added_all=False)
    assert [f.line for f in check_logic_without_tests(ctx)] == [6]


def test_import_only_change_is_fine():
    ctx = make_ctx({"app/x.py": "import os\n"})
    assert check_logic_without_tests(ctx) == []


# --- §3 PII in logs ---


def pii_findings(code: str):
    return [(f.line, f.candidate) for f in check_pii_in_logs(make_ctx({"app/svc.py": code}))]


@pytest.mark.parametrize("call", [
    'logger.info("sent to %s", customer.email)',
    'logger.info(f"sent to {customer.email}")',
    'self.log.warning("x", extra={"who": customer.email})',
    'logger.info("sent to %s", email)',
])
def test_direct_pii_in_log_is_deterministic_s1(call):
    assert pii_findings(f"x = 1\n{call}\n") == [(2, False)]


@pytest.mark.parametrize("call", [
    'logger.info("ctx %s", build_context(customer))',
    'logger.info("ctx %s", customer)',
    'logger.info("ctx %s", customer.to_dict())',
    'logger.info("ctx %s", redact({"contact": customer.email}))',
])
def test_indirect_pii_in_log_is_candidate(call):
    assert pii_findings(call) == [(1, True)]


@pytest.mark.parametrize("call", [
    'logger.info("ok %s", redact({"email": customer.email, "id": customer.id}))',
    'logger.info("ok %s", customer.id)',
    'logger.info("lesson %s", course)',
    'print(customer.email)',
])
def test_safe_log_calls_are_fine(call):
    assert pii_findings(call) == []


def test_pii_check_only_reports_added_lines():
    base = 'logger.info("old %s", customer.email)\n'
    head = base + 'logger.info("new %s", customer.id)\n'
    ctx = make_ctx({"app/svc.py": head}, base_files={"app/svc.py": base}, added_all=False)
    assert check_pii_in_logs(ctx) == []


def test_multiline_log_call_counts_if_any_line_added():
    base = 'logger.info(\n    "x %s",\n    customer.id,\n)\n'
    head = 'logger.info(\n    "x %s",\n    customer.email,\n)\n'
    ctx = make_ctx({"app/svc.py": head}, base_files={"app/svc.py": base}, added_all=False)
    assert [(f.line, f.candidate) for f in check_pii_in_logs(ctx)] == [(1, False)]


# --- §4 retention ---


def test_new_dataset_without_retention_is_candidate():
    code = '"""doc"""\nEVENTS: list[dict] = []\nLIMIT = 10\n'
    findings = check_retention(make_ctx({"app/events.py": code}))
    assert [(f.severity, f.rule, f.line, f.candidate) for f in findings] == [("S1", "4", 2, True)]


def test_new_dataset_with_new_retention_key_is_fine():
    privacy = PRIVACY.replace('{"progress": 365}', '{"progress": 365, "events": 30}')
    ctx = make_ctx({"app/events.py": "EVENTS = []\n", "app/privacy.py": privacy})
    assert check_retention(ctx) == []


def test_non_empty_constants_are_not_datasets():
    assert check_retention(make_ctx({"app/consts.py": 'LEVELS = ["A1", "A2"]\nDEFAULTS = dict(a=1)\n'})) == []


# --- ruff / pytest output parsing ---


def test_parse_ruff_output_keeps_only_touched_files(tmp_path):
    output = (
        f'[{{"filename": "{tmp_path}/app/x.py", "code": "F401", "message": "unused import", "location": {{"row": 3}}}},'
        f' {{"filename": "{tmp_path}/app/old.py", "code": "F401", "message": "unused", "location": {{"row": 1}}}}]'
    )
    findings = parse_ruff_output(output, str(tmp_path), {"app/x.py"})
    assert [(f.severity, f.file, f.line) for f in findings] == [("S3", "app/x.py", 3)]


def test_parse_junit_reports_failures_only():
    xml = (
        '<testsuites><testsuite>'
        '<testcase file="tests/test_a.py" line="9" name="test_ok"/>'
        '<testcase file="tests/test_a.py" line="19" name="test_bad"><failure message="x"/></testcase>'
        '</testsuite></testsuites>'
    )
    assert [(f.severity, f.file, f.line) for f in parse_junit(xml)] == [("S2", "tests/test_a.py", 20)]


def test_pii_fields_removed_by_the_pr_still_count():
    weakened = PRIVACY.replace('"email", ', "")
    ctx = make_ctx({"app/privacy.py": weakened, "app/svc.py": 'logger.info("x %s", customer.email)\n'})
    ctx.files.pop("app/privacy.py")  # only svc.py is under review; privacy.py differs at head
    assert [f.candidate for f in check_pii_in_logs(ctx)] == [False]

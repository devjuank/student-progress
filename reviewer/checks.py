"""Deterministic checks (SPEC.md §3). Each check returns a list of findings.

Explanations never quote the offending values: the gate follows the rules it enforces.
"""
import ast
import json
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from reviewer.diff import PRContext
from reviewer.schema import Finding

PRIVACY_MODULE = "app/privacy.py"
MODELS_MODULE = "app/models.py"
APP_DIR = "app/"
TESTS_DIR = "tests/"
DEFAULT_PII_FIELDS = {"full_name", "email", "birthdate"}
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}

SECRET_PATTERNS = [
    re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),  # SendGrid
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # OpenAI / Anthropic style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b\w*(?:api_?key|secret|token|passw(?:or)?d)\w*\s*[:=]\s*[\"'][^\"'\s]{16,}[\"']"
    ),
]
PLACEHOLDER = re.compile(r"(?i)example|placeholder|your[_-]|xxxx|<[^>]+>|changeme|dummy")


def _finding(severity, rule, file, line, explanation, suggested_fix, candidate=False) -> Finding:
    return Finding(severity, rule, file, line, explanation, suggested_fix, "deterministic", candidate=candidate)


def _is_python_source(path: str) -> bool:
    return path.endswith(".py") and not path.startswith(TESTS_DIR)


def _parse(source: str | None) -> ast.Module | None:
    if source is None:
        return None
    try:
        return ast.parse(source)
    except SyntaxError:
        return None  # ruff reports syntax errors


# --- §5 Secrets ---------------------------------------------------------------


def check_secrets(ctx: PRContext) -> list[Finding]:
    findings = []
    for change in ctx.files.values():
        for line_no, text in change.added.items():
            if any(p.search(text) for p in SECRET_PATTERNS) and not PLACEHOLDER.search(text):
                findings.append(_finding(
                    "S1", "5", change.path, line_no,
                    "A hardcoded credential is committed in this line. A committed secret is a security "
                    "incident regardless of environment.",
                    "Read it from an environment variable, remove it from history and rotate the credential.",
                ))
    return findings


# --- §2 Logic change without tests ----------------------------------------------


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)


def _logic_line(tree: ast.Module, added: dict[int, str]) -> int | None:
    """First added line of real code, preferring lines inside a function.

    Imports, docstrings, comments and blank lines are skipped, so the finding points where a
    reviewer would (e.g. the changed call or the new `def`), not at a module docstring.
    """
    statements = [n for n in ast.walk(tree) if isinstance(n, ast.stmt)]
    functions = [n for n in statements if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    code_lines = []
    for line in added:
        containing = [n for n in statements if n.lineno <= line <= (n.end_lineno or n.lineno)]
        if not containing:
            continue  # blank line or comment between statements
        innermost = min(containing, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
        if not isinstance(innermost, (ast.Import, ast.ImportFrom)) and not _is_docstring(innermost):
            code_lines.append(line)
    in_function = [n for n in code_lines if any(f.lineno <= n <= (f.end_lineno or f.lineno) for f in functions)]
    return min(in_function or code_lines, default=None)


def check_logic_without_tests(ctx: PRContext) -> list[Finding]:
    if any(path.startswith(TESTS_DIR) for path in ctx.files):
        return []
    findings = []
    for change in ctx.files.values():
        if not (change.path.startswith(APP_DIR) and change.path.endswith(".py")):
            continue
        tree = _parse(ctx.head(change.path))
        line = _logic_line(tree, change.added) if tree else min(change.added, default=None)
        if line is not None:
            findings.append(_finding(
                "S2", "2", change.path, line,
                "Code in app/ changes but the PR does not add or modify any test.",
                "Add tests that fail if this behavior breaks, including edge cases.",
                candidate=True,
            ))
    return findings


# --- §3 PII in log calls --------------------------------------------------------


def _string_set_literal(tree: ast.Module | None, name: str) -> set[str] | None:
    """Value of a module-level `NAME = {"a", "b"}` (set/list/tuple of strings), if present."""
    for node in (tree.body if tree else []):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return {v for v in value if isinstance(v, str)}
    return None


def _snake_case(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _pii_object_names(models: ast.Module | None, pii_fields: set[str]) -> set[str]:
    """Variable names that conventionally hold a model with PII fields (Student -> student, students)."""
    names = set()
    for node in (models.body if models else []):
        if not isinstance(node, ast.ClassDef):
            continue
        fields = {s.target.id for s in node.body if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)}
        if fields & pii_fields:
            snake = _snake_case(node.name)
            names |= {snake, snake + "s"}
    return names


def _is_log_call(node: ast.AST) -> bool:
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr not in LOG_METHODS:
        return False
    receiver = node.func.value
    name = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", "")
    return "log" in name.lower()


def _is_redact_call(node: ast.AST) -> bool:
    func = getattr(node, "func", None)
    return isinstance(node, ast.Call) and (
        (isinstance(func, ast.Name) and func.id == "redact") or (isinstance(func, ast.Attribute) and func.attr == "redact")
    )


def _references_pii(node: ast.AST, pii_fields: set[str]) -> bool:
    return any(
        (isinstance(n, ast.Attribute) and n.attr in pii_fields) or (isinstance(n, ast.Name) and n.id in pii_fields)
        for n in ast.walk(node)
    )


def _classify_log_arg(node: ast.AST, pii_fields: set[str], pii_objects: set[str]) -> str | None:
    """Returns "direct" (PII reaches the log unredacted), "candidate" (needs semantic review) or None."""
    if _is_redact_call(node):
        payload = node.args[0] if node.args else None
        if isinstance(payload, ast.Dict):
            for key, value in zip(payload.keys, payload.values):
                is_masked_key = isinstance(key, ast.Constant) and key.value in pii_fields
                if not is_masked_key and _references_pii(value, pii_fields):
                    return "candidate"  # redact() only masks exact PII key names
        return None
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)
    if name in pii_fields:
        return "direct"
    if name in pii_objects:
        return "candidate"  # object serialization or helper receiving the whole object
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in pii_objects:
        return None  # non-PII attribute of a PII object, e.g. student.id
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id in pii_objects:
            return "candidate"  # method on a PII object, e.g. student.to_dict()
    verdicts = [_classify_log_arg(child, pii_fields, pii_objects) for child in ast.iter_child_nodes(node)]
    if "direct" in verdicts:
        return "direct"
    if "candidate" in verdicts:
        return "candidate"
    return None


def check_pii_in_logs(ctx: PRContext) -> list[Finding]:
    pii_fields = _string_set_literal(_parse(ctx.head(PRIVACY_MODULE)), "PII_FIELDS") or DEFAULT_PII_FIELDS
    pii_objects = _pii_object_names(_parse(ctx.head(MODELS_MODULE)), pii_fields)
    findings = []
    for change in ctx.files.values():
        if not (_is_python_source(change.path) and change.added):
            continue
        for node in ast.walk(_parse(ctx.head(change.path)) or ast.Module(body=[], type_ignores=[])):
            if not _is_log_call(node):
                continue
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            if not any(n in change.added for n in span):
                continue
            args = [*node.args, *(kw.value for kw in node.keywords)]
            verdicts = {_classify_log_arg(arg, pii_fields, pii_objects) for arg in args}
            if "direct" in verdicts:
                findings.append(_finding(
                    "S1", "3", change.path, node.lineno,
                    "A PII field is passed to a log call without redaction.",
                    "Log a non-personal identifier (e.g. student id) or wrap the payload in app.privacy.redact().",
                ))
            elif "candidate" in verdicts:
                findings.append(_finding(
                    "S1", "3", change.path, node.lineno,
                    "A log call receives an object or value that may carry PII past redact() "
                    "(object serialization, helper output or a PII value under a non-PII key).",
                    "Log only non-personal identifiers, or pass an explicit dict through app.privacy.redact().",
                    candidate=True,
                ))
    return findings


# --- §4 Retention ----------------------------------------------------------------


def _retention_keys(tree: ast.Module | None) -> set[str]:
    for node in (tree.body if tree else []):
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(isinstance(t, ast.Name) and t.id == "RETENTION_DAYS" for t in targets):
            if isinstance(node.value, ast.Dict):
                return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    return set()


def _is_empty_collection(node: ast.AST | None) -> bool:
    if isinstance(node, (ast.List, ast.Set)) and not node.elts:
        return True
    if isinstance(node, ast.Dict) and not node.keys:
        return True
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
        return False
    if node.func.id == "defaultdict":
        return True
    return node.func.id in {"list", "dict", "set", "deque", "OrderedDict"} and not node.args and not node.keywords


def check_retention(ctx: PRContext) -> list[Finding]:
    """A new module-level collection that accumulates data is a dataset (TEAM-STANDARDS §4)."""
    new_keys = _retention_keys(_parse(ctx.head(PRIVACY_MODULE))) - _retention_keys(_parse(ctx.base(PRIVACY_MODULE)))
    if new_keys:
        return []
    findings = []
    for change in ctx.files.values():
        if not _is_python_source(change.path) or change.path == PRIVACY_MODULE:
            continue
        for node in (_parse(ctx.head(change.path)) or ast.Module(body=[], type_ignores=[])).body:
            value = getattr(node, "value", None)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.lineno in change.added and _is_empty_collection(value):
                findings.append(_finding(
                    "S1", "4", change.path, node.lineno,
                    "New module-level dataset without a retention category in app.privacy.RETENTION_DAYS.",
                    "Declare its retention category in RETENTION_DAYS (90 days max for minors' data) and "
                    "store only the fields the purpose needs.",
                    candidate=True,
                ))
    return findings


# --- ruff and pytest ----------------------------------------------------------------


def parse_ruff_output(output: str, workdir: str, touched: set[str]) -> list[Finding]:
    findings = []
    for item in json.loads(output or "[]"):
        path = Path(item["filename"]).resolve().relative_to(Path(workdir).resolve()).as_posix()
        if path in touched:
            findings.append(_finding(
                "S3", "1", path, item["location"]["row"],
                f"ruff {item['code']}: {item['message']}",
                "Fix the lint error (`ruff check --fix` handles many of them).",
            ))
    return findings


def run_ruff(ctx: PRContext, workdir: str) -> list[Finding]:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "json", "--exit-zero", "."],
        cwd=workdir, capture_output=True, text=True, check=True,
    )
    return parse_ruff_output(result.stdout, workdir, set(ctx.files))


def parse_junit(xml_text: str) -> list[Finding]:
    findings = []
    for case in ET.fromstring(xml_text).iter("testcase"):
        if case.find("failure") is None and case.find("error") is None:
            continue
        findings.append(_finding(
            "S2", "2", case.get("file") or "tests", int(case.get("line") or 0) + 1,
            f"Test {case.get('name')} fails on the PR head.",
            "Fix the regression or update the test if the expected behavior changed on purpose.",
        ))
    return findings


def run_pytest(workdir: str) -> list[Finding]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "junit.xml"
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "junit_family=xunit1", f"--junitxml={report}"],
            cwd=workdir, capture_output=True, text=True,
        )
        if result.returncode in (0, 5):  # 5 = no tests collected
            return []
        findings = parse_junit(report.read_text()) if report.exists() else []
    return findings or [_finding(
        "S2", "2", "tests", 1,
        f"The test suite could not run (pytest exit code {result.returncode}).",
        "Run pytest locally and fix the collection or import error.",
    )]


def run_static_checks(ctx: PRContext) -> list[Finding]:
    """Checks that only read the diff and file contents (no code execution)."""
    return [
        *check_secrets(ctx),
        *check_logic_without_tests(ctx),
        *check_pii_in_logs(ctx),
        *check_retention(ctx),
    ]

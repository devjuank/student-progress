"""Eval scoring against a synthetic ground truth (not the golden set)."""
from reviewer.eval import Case, Issue, decision_accuracy, load_ground_truth, metrics, score_case

GT_YAML = """
- pr: feature/a
  expected_decision: block
  issues:
    - id: a-1
      severity: S1
      rule: "3"
      file: app/x.py
      line: 20
      description: leak
      ambiguous: false
      also_at: ["app/y.py:7"]
    - id: a-2
      severity: S2
      rule: "2"
      file: tests/test_x.py
      line: 5
      ambiguous: true
- pr: feature/b
  expected_decision:
  issues: []
"""


def finding(rule="3", file="app/x.py", line=20, severity="S1", source="llm") -> dict:
    return {"rule": rule, "file": file, "line": line, "severity": severity, "source": source}


def issue(id="i", rule="3", file="app/x.py", line=20, severity="S1", ambiguous=False) -> Issue:
    return Issue(id, severity, rule, file, line, ambiguous, [(file, line)])


def test_load_ground_truth(tmp_path):
    path = tmp_path / "gt.yaml"
    path.write_text(GT_YAML)
    cases = load_ground_truth(str(path))
    assert [c.branch for c in cases] == ["feature/a", "feature/b"]
    assert cases[0].issues[0].locations == [("app/x.py", 20), ("app/y.py", 7)]
    assert cases[0].issues[1].ambiguous and cases[1].expected_decision is None


def test_match_requires_same_rule_and_line_within_tolerance():
    issues = [issue()]
    assert score_case([finding(line=25)], issues)["matched"] == ["i"]
    assert score_case([finding(line=26)], issues)["matched"] == []
    assert score_case([finding(rule="4")], issues)["matched"] == []
    assert score_case([finding(file="app/other.py")], issues)["matched"] == []


def test_also_at_location_matches():
    i = issue()
    i.locations.append(("app/y.py", 7))
    assert score_case([finding(file="app/y.py", line=9)], [i])["matched"] == ["i"]


def test_duplicates_are_ignored_not_false_positives():
    result = score_case([finding(), finding(line=22)], [issue()])
    assert [o.get("duplicate", False) for o in result["outcomes"]] == [False, True]


def test_two_findings_match_two_issues_on_close_lines():
    result = score_case([finding(line=20), finding(line=22)], [issue("a", line=20), issue("b", line=23)])
    assert result["matched"] == ["a", "b"]


def test_metrics_strict_excludes_ambiguous_inclusive_counts_them():
    cases = [Case("pr", "block", [issue("real"), issue("amb", rule="2", line=50, ambiguous=True)])]
    scored = {"pr": score_case([finding(severity="S2"), finding(rule="4", line=90)], cases[0].issues)}
    m = metrics(cases, scored)
    assert (m["strict"]["tp"], m["strict"]["fp"], m["strict"]["fn"]) == (1, 1, 0)
    assert (m["inclusive"]["tp"], m["inclusive"]["fn"]) == (1, 1)
    assert m["strict"]["severity_accuracy"] == 0.0
    assert m["strict"]["per_source"]["llm"]["precision"] == 0.5


def test_matching_an_ambiguous_issue_is_neither_tp_nor_fp_in_strict_view():
    cases = [Case("pr", "block", [issue("amb", ambiguous=True)])]
    m = metrics(cases, {"pr": score_case([finding()], cases[0].issues)})
    assert (m["strict"]["tp"], m["strict"]["fp"], m["strict"]["fn"]) == (0, 0, 0)
    assert m["inclusive"]["tp"] == 1


def test_decision_accuracy_two_columns_and_missing_expected():
    cases = [Case("a", "block", []), Case("b", "approve", []), Case("c", None, [])]
    results = {
        "a": {"decision": "review", "standards_decision": "block"},
        "b": {"decision": "comment", "standards_decision": "comment"},
        "c": {"decision": "approve", "standards_decision": "approve"},
    }
    d = decision_accuracy(cases, results)
    assert d["standards"] == 0.5  # b expected approve, got comment
    assert d["gate_stopped"] == 1.0  # a stopped (review), b let through
    assert d["missing_expected_decision"] == ["c"]

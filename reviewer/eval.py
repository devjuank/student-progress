"""Eval harness (SPEC.md §7): runs the reviewer over the golden set and scores it.

python -m reviewer.eval [--runs N] [--no-llm] [--only <branch>]
"""
import argparse
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from reviewer import cli, llm

GROUND_TRUTH = "eval/ground_truth.yaml"
DESCRIPTIONS = "PULL_REQUESTS.md"
HISTORY = "eval/eval_history.jsonl"
RUNS_DIR = "eval/runs"
MATCH_LINES = 5
STOPPED = {"block", "review"}


@dataclass
class Issue:
    id: str
    severity: str
    rule: str
    file: str
    line: int
    ambiguous: bool = False
    locations: list[tuple[str, int]] = field(default_factory=list)  # file:line plus also_at


@dataclass
class Case:
    branch: str
    expected_decision: str | None
    issues: list[Issue]


def _location(text: str) -> tuple[str, int]:
    path, _, line = str(text).rpartition(":")
    return path, int(line)


def load_ground_truth(path: str = GROUND_TRUTH) -> list[Case]:
    cases = []
    for entry in yaml.safe_load(Path(path).read_text()):
        issues = []
        for raw in entry.get("issues") or []:
            issue = Issue(
                id=str(raw["id"]), severity=raw["severity"], rule=str(raw["rule"]),
                file=raw["file"], line=int(raw["line"]), ambiguous=bool(raw.get("ambiguous", False)),
            )
            issue.locations = [(issue.file, issue.line), *map(_location, raw.get("also_at") or [])]
            issues.append(issue)
        cases.append(Case(entry["pr"], entry.get("expected_decision") or None, issues))
    return cases


def load_descriptions(path: str = DESCRIPTIONS) -> dict[str, str]:
    """Author descriptions from the PR table: | # | `branch` | title | "description" |."""
    descriptions = {}
    for match in re.finditer(r"^\|\s*\d+\s*\|\s*`([^`]+)`\s*\|[^|]*\|\s*\"?(.*?)\"?\s*\|$", Path(path).read_text(), re.M):
        descriptions[match.group(1)] = match.group(2)
    return descriptions


def _matches(finding: dict, issue: Issue) -> bool:
    return str(finding["rule"]) == issue.rule and any(
        finding["file"] == path and abs(finding["line"] - line) <= MATCH_LINES for path, line in issue.locations
    )


def score_case(findings: list[dict], issues: list[Issue]) -> dict:
    """Matches findings to issues. Each issue matches at most one finding; duplicates are ignored.

    Returns per-finding and per-issue outcomes for both the strict view (ambiguous issues excluded)
    and the inclusive view (ambiguous issues count as real).
    """
    matched: dict[str, dict] = {}
    outcomes = []
    for finding in findings:
        candidates = [i for i in issues if _matches(finding, i)]
        free = sorted((i for i in candidates if i.id not in matched), key=lambda i: i.ambiguous)
        if free:
            matched[free[0].id] = finding
            outcomes.append({"finding": finding, "issue": free[0].id, "ambiguous": free[0].ambiguous,
                             "severity_ok": finding["severity"] == free[0].severity})
        elif candidates:
            outcomes.append({"finding": finding, "issue": candidates[0].id, "duplicate": True})
        else:
            outcomes.append({"finding": finding, "issue": None})
    missed = [i.id for i in issues if i.id not in matched]
    return {"outcomes": outcomes, "matched": sorted(matched), "missed": missed}


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def metrics(cases: list[Case], scored: dict[str, dict]) -> dict:
    """Precision/recall overall, per severity and per source, plus severity accuracy."""
    by_id = {i.id: i for c in cases for i in c.issues}
    out = {}
    for view in ("strict", "inclusive"):
        tp = fp = fn = sev_ok = 0
        per_sev = {s: Counter() for s in ("S1", "S2", "S3")}
        per_src = {s: Counter() for s in ("deterministic", "llm")}
        for case in cases:
            result = scored[case.branch]
            for o in result["outcomes"]:
                if o.get("duplicate"):
                    continue
                f = o["finding"]
                if o["issue"] is None:
                    fp += 1
                    per_sev[f["severity"]]["fp"] += 1
                    per_src[f["source"]]["fp"] += 1
                elif o["ambiguous"] and view == "strict":
                    continue
                else:
                    tp += 1
                    sev_ok += o["severity_ok"]
                    per_sev[by_id[o["issue"]].severity]["tp"] += 1
                    per_src[f["source"]]["tp"] += 1
            for issue_id in result["missed"]:
                issue = by_id[issue_id]
                if issue.ambiguous and view == "strict":
                    continue
                fn += 1
                per_sev[issue.severity]["fn"] += 1
        out[view] = {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": _ratio(tp, tp + fp), "recall": _ratio(tp, tp + fn),
            "severity_accuracy": _ratio(sev_ok, tp),
            "per_severity": {s: {"precision": _ratio(c["tp"], c["tp"] + c["fp"]),
                                 "recall": _ratio(c["tp"], c["tp"] + c["fn"]), **c} for s, c in per_sev.items()},
            "per_source": {s: {"precision": _ratio(c["tp"], c["tp"] + c["fp"]), **c} for s, c in per_src.items()},
        }
    return out


def decision_accuracy(cases: list[Case], results: dict[str, dict]) -> dict:
    """Two columns (SPEC §7): standards decision vs expected, and gate stopped/let-through vs expected."""
    rows = [c for c in cases if c.expected_decision]
    standards = sum(results[c.branch]["standards_decision"] == c.expected_decision for c in rows)
    gate = sum((results[c.branch]["decision"] in STOPPED) == (c.expected_decision == "block") for c in rows)
    return {
        "scored_prs": len(rows),
        "missing_expected_decision": [c.branch for c in cases if not c.expected_decision],
        "standards": _ratio(standards, len(rows)),
        "gate_stopped": _ratio(gate, len(rows)),
    }


def run_eval(cases: list[Case], runs: int, use_llm: bool, base: str = "main") -> dict:
    descriptions = load_descriptions()
    per_run = []
    for n in range(runs):
        results, scored = {}, {}
        for case in cases:
            print(f"[run {n + 1}/{runs}] {case.branch}", file=sys.stderr)
            results[case.branch] = cli.run(base, case.branch, use_llm=use_llm,
                                           pr_description=descriptions.get(case.branch, ""))
            scored[case.branch] = score_case(results[case.branch]["findings"], case.issues)
        per_run.append({"results": results, "scored": scored, "metrics": metrics(cases, scored),
                        "decisions": decision_accuracy(cases, results)})
    return summarize(cases, per_run)


def _stats(values: list[float]) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"mean": None}
    return {"mean": round(statistics.mean(values), 4), "min": round(min(values), 4), "max": round(max(values), 4)}


def summarize(cases: list[Case], per_run: list[dict]) -> dict:
    llm_meta = [r["llm"] for run in per_run for r in run["results"].values() if r["llm"]]
    stability = {
        c.branch: dict(Counter(run["results"][c.branch]["decision"] for run in per_run)) for c in cases
    }
    return {
        "runs": len(per_run),
        "precision": _stats([r["metrics"]["strict"]["precision"] for r in per_run]),
        "recall": _stats([r["metrics"]["strict"]["recall"] for r in per_run]),
        "gate_decision_accuracy": _stats([r["decisions"]["gate_stopped"] for r in per_run]),
        "standards_decision_accuracy": _stats([r["decisions"]["standards"] for r in per_run]),
        "cost_usd_per_pr": _stats([m["cost_usd"] for m in llm_meta]),
        "latency_s_per_pr": _stats([m["latency_s"] for m in llm_meta]),
        "llm_errors": sum(bool(r["llm_error"]) for run in per_run for r in run["results"].values()),
        "decision_stability": stability,
        "per_run": per_run,
    }


def print_report(cases: list[Case], summary: dict) -> None:
    last = summary["per_run"][-1]
    print(f"\n{'PR':32} {'expected':9} {'gate':8} {'standards':10} TP FP FN(missed)")
    for case in cases:
        result, scored = last["results"][case.branch], last["scored"][case.branch]
        tp = sum(1 for o in scored["outcomes"] if o["issue"] and not o.get("duplicate"))
        fp = sum(1 for o in scored["outcomes"] if o["issue"] is None)
        print(f"{case.branch:32} {case.expected_decision or '-':9} {result['decision']:8} "
              f"{result['standards_decision']:10} {tp:2} {fp:2} {len(scored['missed']):2} {scored['missed']}")
    m, d = last["metrics"], last["decisions"]
    print(f"\nstrict    P={m['strict']['precision']} R={m['strict']['recall']} "
          f"sev_acc={m['strict']['severity_accuracy']} (tp={m['strict']['tp']} fp={m['strict']['fp']} fn={m['strict']['fn']})")
    print(f"inclusive P={m['inclusive']['precision']} R={m['inclusive']['recall']}")
    print(f"decisions: standards={d['standards']} gate_stopped={d['gate_stopped']} over {d['scored_prs']} PRs"
          + (f"; missing expected_decision: {d['missing_expected_decision']}" if d["missing_expected_decision"] else ""))
    if summary["runs"] > 1:
        print(f"stability: {summary['decision_stability']}")
    print(f"cost/PR: {summary['cost_usd_per_pr']}  latency/PR: {summary['latency_s_per_pr']}  "
          f"llm_errors: {summary['llm_errors']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reviewer.eval", description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--no-llm", action="store_true", help="score deterministic checks only")
    parser.add_argument("--only", help="evaluate a single branch")
    parser.add_argument("--note", default="", help="free text saved in the history (what changed)")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    cases = [c for c in load_ground_truth() if not args.only or c.branch == args.only]
    summary = run_eval(cases, args.runs, use_llm=not args.no_llm)
    print_report(cases, summary)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    model = "none" if args.no_llm else llm.os.environ.get("REVIEWER_MODEL", llm.DEFAULT_MODEL)
    Path(RUNS_DIR).mkdir(parents=True, exist_ok=True)
    Path(RUNS_DIR, f"{stamp}.json").write_text(json.dumps(summary, indent=1, default=str))
    history = {
        "ts": stamp, "git_sha": sha, "model": model, "prompt_version": llm.PROMPT_VERSION,
        "note": args.note, "prs": len(cases),
        **{k: v for k, v in summary.items() if k != "per_run"},
    }
    with open(HISTORY, "a") as handle:
        handle.write(json.dumps(history) + "\n")
    print(f"\nsaved {RUNS_DIR}/{stamp}.json and appended {HISTORY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

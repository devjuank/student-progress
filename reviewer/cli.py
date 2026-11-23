"""Entry point: python -m reviewer --base main --head <branch> [--dry-run] [--no-llm]."""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict

from reviewer import llm, policy, telemetry
from reviewer.checks import run_pytest, run_ruff, run_static_checks
from reviewer.diff import load_pr


@contextmanager
def head_checkout(head: str, repo: str):
    """Temporary git worktree of head, so ruff and pytest run on the PR's code."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "worktree", "add", "-q", "--detach", tmp, head], cwd=repo, check=True)
        try:
            yield tmp
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", tmp], cwd=repo, check=True)


def run(base: str, head: str, repo: str = ".", use_llm: bool = True, workdir: str | None = None,
        pr_description: str = "") -> dict:
    start = time.monotonic()
    ctx = load_pr(base, head, repo)
    if workdir:
        deterministic = run_static_checks(ctx) + run_ruff(ctx, workdir) + run_pytest(workdir)
    else:
        with head_checkout(head, repo) as checkout:
            deterministic = run_static_checks(ctx) + run_ruff(ctx, checkout) + run_pytest(checkout)

    llm_result, llm_error = None, None
    if use_llm:
        diff_text = subprocess.run(
            ["git", "diff", "--no-color", ctx.base_ref, head], cwd=repo, capture_output=True, text=True, check=True
        ).stdout
        candidates = [f for f in deterministic if f.candidate]
        try:
            llm_result = llm.review(ctx, diff_text, candidates, pr_description, repo)
        except Exception as error:  # the gate never approves when the LLM fails (SPEC §6)
            llm_error = f"{type(error).__name__}: {error}"

    llm_findings = llm_result.findings if llm_result else []
    # Without the LLM, candidates cannot be confirmed; keep them visible as findings to review.
    unconfirmed = [] if use_llm and llm_result else [f for f in deterministic if f.candidate]
    findings = policy.merge(deterministic, llm_findings) + unconfirmed
    decision = policy.decide(findings, llm_failed=use_llm and llm_result is None)

    llm_meta = llm.result_metadata(llm_result) if llm_result else None
    telemetry.emit(telemetry.build_event(
        head, decision, findings, deterministic, llm_meta, llm_error, time.monotonic() - start
    ))
    return {
        "head": head,
        "decision": decision,
        "standards_decision": policy.standards_decision(findings),
        "findings": [asdict(f) for f in findings],
        "llm": llm_meta,
        "llm_error": llm_error,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reviewer", description=__doc__)
    parser.add_argument("--base", default="main")
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--workdir", help="existing checkout of head (CI); default: temporary worktree")
    parser.add_argument("--description", default="", help="PR description given to the LLM")
    parser.add_argument("--no-llm", action="store_true", help="deterministic checks only")
    parser.add_argument("--dry-run", action="store_true", help="print the result; never post to GitHub")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    result = run(args.base, args.head, args.repo, not args.no_llm, args.workdir, args.description)
    print(json.dumps(result, indent=2))
    return 1 if result["decision"] == "block" else 0


if __name__ == "__main__":
    sys.exit(main())

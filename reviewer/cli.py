"""Entry point: python -m reviewer --base main --head <branch> [--dry-run] [--no-llm].

CI splits the run in two jobs (see .github/workflows/quality-gate.yml):
  --tools-only --tools-out F   run ruff + pytest on the PR's code (job without secrets)
  --tools-in F                 reuse those results; never executes the PR's code (job with the API key)
"""
import argparse
import json
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from reviewer import llm, policy, report, telemetry
from reviewer.checks import run_pytest, run_ruff, run_static_checks
from reviewer.diff import PRContext, load_pr
from reviewer.schema import Finding


@contextmanager
def head_checkout(head: str, repo: str):
    """Temporary git worktree of head, so ruff and pytest run on the PR's code."""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["git", "worktree", "add", "-q", "--detach", tmp, head], cwd=repo, check=True)
        try:
            yield tmp
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", tmp], cwd=repo, check=True)


def run_tools(ctx: PRContext, head: str, repo: str, workdir: str | None) -> list[Finding]:
    if workdir:
        return run_ruff(ctx, workdir) + run_pytest(workdir)
    with head_checkout(ctx.head_ref, repo) as checkout:
        return run_ruff(ctx, checkout) + run_pytest(checkout)


def run(base: str, head: str, repo: str = ".", use_llm: bool = True, workdir: str | None = None,
        pr_description: str = "", tools: list[Finding] | None = None) -> dict:
    """Full review. `tools` are precomputed ruff/pytest findings (CI); otherwise they run here."""
    start = time.monotonic()
    ctx = load_pr(base, head, repo)
    if tools is None:
        tools = run_tools(ctx, head, repo, workdir)
    deterministic = run_static_checks(ctx) + tools

    llm_result, llm_error = None, None
    if use_llm:
        diff_text = subprocess.run(
            ["git", "diff", "--no-color", ctx.base_ref, ctx.head_ref], cwd=repo, capture_output=True, text=True, check=True
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
    parser = argparse.ArgumentParser(prog="reviewer", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="main")
    parser.add_argument("--head", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--workdir", help="existing checkout of head; default: temporary worktree")
    parser.add_argument("--description", default="", help="PR description given to the LLM")
    parser.add_argument("--description-file", help="read the PR description from a file")
    parser.add_argument("--no-llm", action="store_true", help="deterministic checks only")
    parser.add_argument("--tools-only", action="store_true", help="only run ruff + pytest (needs --tools-out)")
    parser.add_argument("--tools-out", help="write ruff + pytest findings as JSON")
    parser.add_argument("--tools-in", help="read ruff + pytest findings from JSON instead of running them")
    parser.add_argument("--report", help="write the PR comment (Markdown) to this file")
    parser.add_argument("--dry-run", action="store_true", help="print the result; never post to GitHub")
    args = parser.parse_args(argv)

    if args.tools_only:
        if not args.tools_out:
            parser.error("--tools-only requires --tools-out")
        ctx = load_pr(args.base, args.head, args.repo)
        tools = run_tools(ctx, args.head, args.repo, args.workdir)
        Path(args.tools_out).write_text(json.dumps([asdict(f) for f in tools], indent=1))
        print(f"{len(tools)} tool finding(s) written to {args.tools_out}")
        return 0

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    tools = None
    if args.tools_in:
        tools = [Finding(**f) for f in json.loads(Path(args.tools_in).read_text())]
    description = Path(args.description_file).read_text() if args.description_file else args.description

    result = run(args.base, args.head, args.repo, not args.no_llm, args.workdir, description, tools)
    if args.report:
        Path(args.report).write_text(report.render(result))
    print(json.dumps(result, indent=2))
    if result["decision"] == "review":
        print("::warning title=AI Quality Gate::Human review requested; see the PR comment.")
    return 1 if result["decision"] == "block" else 0


if __name__ == "__main__":
    sys.exit(main())

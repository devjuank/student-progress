"""Merge, dedupe and decision policy (SPEC.md §5 and §6)."""
from reviewer.schema import Finding

DEDUPE_LINES = 3
BLOCK_CONFIDENCE = 0.8
SEVERITY_ORDER = {"S1": 0, "S2": 1, "S3": 2}


def merge(deterministic: list[Finding], llm: list[Finding]) -> list[Finding]:
    """Combines both sources. Candidates are dropped: the LLM re-reports the ones it confirms.

    Duplicates share file and rule with lines within ±DEDUPE_LINES; the deterministic finding wins,
    keeping the higher severity of the pair.
    """
    merged = [f for f in deterministic if not f.candidate]
    for finding in llm:
        duplicate = next(
            (m for m in merged
             if m.file == finding.file and m.rule == finding.rule and abs(m.line - finding.line) <= DEDUPE_LINES),
            None,
        )
        if duplicate is None:
            merged.append(finding)
        elif SEVERITY_ORDER[finding.severity] < SEVERITY_ORDER[duplicate.severity]:
            duplicate.severity = finding.severity
    return sorted(merged, key=lambda f: (SEVERITY_ORDER[f.severity], f.file, f.line))


def decide(findings: list[Finding], llm_failed: bool = False) -> str:
    """Gate decision after the trust policy: block, review, comment or approve."""
    blocking = [f for f in findings if f.severity in ("S1", "S2")]
    if any(f.source == "deterministic" and not f.candidate for f in blocking):
        return "block"
    if any(f.severity == "S1" and (f.confidence or 0) >= BLOCK_CONFIDENCE for f in blocking):
        return "block"
    if blocking or llm_failed:
        return "review"
    if findings:
        return "comment"
    return "approve"


def standards_decision(findings: list[Finding]) -> str:
    """Decision TEAM-STANDARDS would take on these findings, before the trust policy."""
    if any(f.severity in ("S1", "S2") for f in findings):
        return "block"
    return "comment" if findings else "approve"

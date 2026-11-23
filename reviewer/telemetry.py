"""Structured run logs (SPEC.md §8): metadata only, no diffs, prompts or finding text."""
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

from reviewer.schema import Finding

TELEMETRY_FILE = os.environ.get("REVIEWER_TELEMETRY_FILE", "telemetry.jsonl")


def build_event(head: str, decision: str, findings: list[Finding], deterministic: list[Finding],
                llm: dict | None, llm_error: str | None, duration_s: float) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "head": head,
        "decision": decision,
        "findings_by_severity": dict(Counter(f.severity for f in findings)),
        "findings_by_source": dict(Counter(f.source for f in findings)),
        "candidates": sum(f.candidate for f in deterministic),
        "llm": llm,
        "llm_error": llm_error.split(":")[0] if llm_error else None,  # error class only, no payload
        "duration_s": round(duration_s, 2),
    }


def emit(event: dict) -> None:
    line = json.dumps(event, sort_keys=True)
    print(line, file=sys.stderr)
    with open(TELEMETRY_FILE, "a") as handle:
        handle.write(line + "\n")

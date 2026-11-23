"""PR comment and job summary (Markdown)."""

MARKER = "<!-- ai-quality-gate -->"

HEADLINES = {
    "block": "🔴 **Blocked** — fix the S1/S2 findings below before merging.",
    "review": "🟡 **Human review requested** — the gate found possible S1/S2 issues it is not "
              "trusted to block on its own (or the LLM review failed).",
    "comment": "💬 **Comments only** — nothing blocks this PR.",
    "approve": "🟢 **No findings.**",
}


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def render(result: dict) -> str:
    lines = [MARKER, "## AI Quality Gate", "", HEADLINES[result["decision"]], ""]
    findings = result["findings"]
    if findings:
        lines += [
            "| Sev | Rule | Location | Source | Finding | Suggested fix |",
            "|-----|------|----------|--------|---------|---------------|",
        ]
        for f in findings:
            source = f["source"]
            if f.get("candidate"):
                source += " (unconfirmed)"
            elif f.get("confidence") is not None:
                source += f" ({f['confidence']:.2f})"
            lines.append(
                f"| {f['severity']} | §{f['rule']} | `{f['file']}:{f['line']}` | {source} "
                f"| {_cell(f['explanation'])} | {_cell(f['suggested_fix'])} |"
            )
        lines.append("")
    if result.get("llm_error"):
        lines += [f"> ⚠️ LLM review unavailable ({result['llm_error'].split(':')[0]}); "
                  "only deterministic checks ran.", ""]
    meta = result.get("llm") or {}
    details = [f"standards decision: `{result['standards_decision']}`"]
    if meta:
        cost = f"${meta['cost_usd']:.4f}" if meta.get("cost_usd") is not None else "n/a"
        details += [f"model: `{meta['model']}`", f"prompt: `{meta['prompt_version']}`",
                    f"cost: {cost}", f"latency: {meta['latency_s']:.1f}s"]
    lines += [f"<sub>{' · '.join(details)} · rules: TEAM-STANDARDS.md</sub>", ""]
    return "\n".join(lines)

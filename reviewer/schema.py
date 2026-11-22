"""Finding schema (SPEC.md §2)."""
from dataclasses import asdict, dataclass

SEVERITIES = {"S1", "S2", "S3"}
RULES = {"1", "2", "3", "4", "5", "6"}
SOURCES = {"deterministic", "llm"}


@dataclass
class Finding:
    severity: str
    rule: str
    file: str
    line: int
    explanation: str
    suggested_fix: str
    source: str
    confidence: float | None = None
    # Deterministic hint that the LLM must confirm or discard (SPEC.md §3).
    candidate: bool = False

    def __post_init__(self):
        errors = validate(asdict(self))
        if errors:
            raise ValueError("; ".join(errors))


def validate(data: dict) -> list[str]:
    """Returns the schema errors of a finding as a dict (empty list if valid)."""
    errors = []
    if data.get("severity") not in SEVERITIES:
        errors.append(f"severity must be one of {sorted(SEVERITIES)}")
    if str(data.get("rule")) not in RULES:
        errors.append(f"rule must be one of {sorted(RULES)}")
    if not isinstance(data.get("file"), str) or not data["file"]:
        errors.append("file must be a non-empty string")
    if not isinstance(data.get("line"), int) or data["line"] < 1:
        errors.append("line must be a positive int")
    for field in ("explanation", "suggested_fix"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            errors.append(f"{field} must be a non-empty string")
    if data.get("source") not in SOURCES:
        errors.append(f"source must be one of {sorted(SOURCES)}")
    confidence = data.get("confidence")
    if confidence is not None and not (isinstance(confidence, (int, float)) and 0 <= confidence <= 1):
        errors.append("confidence must be a number between 0 and 1")
    return errors

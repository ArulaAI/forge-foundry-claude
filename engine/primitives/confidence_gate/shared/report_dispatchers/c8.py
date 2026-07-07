"""C6 dispatcher for C8 ArchitectureReport JSON."""

from __future__ import annotations

from primitives.confidence_gate.shared.models import NormalizedFinding

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}
# C8 layer_violations and subjective issues have severity but no confidence field.
# We default confidence_level to "medium" for these findings.
_DEFAULT_CONFIDENCE: str = "medium"


def sniff(data: dict[str, object]) -> bool:
    """Return True if data looks like a C8 ArchitectureReport."""
    return (
        "code_path" in data
        and "layer_violations" in data
        and "cycles" in data
        and "coupling" in data
    )


def dispatch(data: dict[str, object]) -> list[NormalizedFinding]:
    """Extract NormalizedFindings from a C8 ArchitectureReport JSON dict.

    Pulls from layer_violations and subjective issues.
    """
    findings: list[NormalizedFinding] = []

    # Layer violations
    layer_violations = data.get("layer_violations", [])
    if isinstance(layer_violations, list):
        for lv in layer_violations:
            if not isinstance(lv, dict):
                continue
            severity = lv.get("severity", "")
            if severity not in _VALID_SEVERITIES:
                continue
            findings.append(
                NormalizedFinding(
                    report_type="c8_architecture_review",
                    severity=severity,
                    confidence_level="medium",
                    file=None,
                    line=None,
                    title=(
                        f"Layer violation: {lv.get('from_module', '')} -> {lv.get('to_module', '')}"
                    ),
                    description=str(lv.get("actual_direction", "")),
                )
            )

    # Subjective issues
    subjective = data.get("subjective", [])
    if isinstance(subjective, list):
        for issue in subjective:
            if not isinstance(issue, dict):
                continue
            severity = issue.get("severity", "")
            if severity not in _VALID_SEVERITIES:
                continue
            findings.append(
                NormalizedFinding(
                    report_type="c8_architecture_review",
                    severity=severity,
                    confidence_level="medium",
                    file=None,
                    line=None,
                    title=str(issue.get("kind", "")),
                    description=str(issue.get("description", "")),
                )
            )

    return findings

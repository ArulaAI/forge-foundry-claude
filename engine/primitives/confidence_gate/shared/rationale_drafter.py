"""C6 /confidence-gate — Stage C: rationale drafter.

One LLM call. Returns (rationale_str, top_findings).

Anti-hallucination (S10 lesson):
- report_type Literal validation: drop unknown values
- top_findings must reference findings present in the input data
  (checked by title match in the finding set); drops fabricated entries
  and logs a warning.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from primitives.confidence_gate.shared.models import (
    _ALL_REPORT_TYPES,
    _ALL_SEVERITIES,
    GateScore,
    NormalizedFinding,
    PrimitiveRiskSummary,
    TopFinding,
)
from primitives.confidence_gate.shared.prompts import (
    RATIONALE_SYSTEM_PROMPT,
    RATIONALE_USER_TEMPLATE,
)

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem

logger = logging.getLogger(__name__)


class RationaleResult(NamedTuple):
    """Result of the rationale drafting stage."""

    rationale: str
    top_findings: list[TopFinding]


def _build_summaries_text(summaries: list[PrimitiveRiskSummary]) -> str:
    """Format per-primitive summaries for the LLM prompt."""
    lines: list[str] = []
    for s in summaries:
        sev_parts = ", ".join(f"{sev}={cnt}" for sev, cnt in s.by_severity.items() if cnt > 0)
        lines.append(
            f"  {s.report_type}: {s.total_findings} findings "
            f"({sev_parts}), score={s.contributed_score:.2f}"
        )
    return "\n".join(lines) if lines else "  (no findings)"


def _build_findings_text(findings: list[NormalizedFinding], max_findings: int = 20) -> str:
    """Format sample findings for the LLM prompt (highest severity first)."""
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    sorted_findings = sorted(findings, key=lambda f: sev_order.get(f.severity, 9))
    sample = sorted_findings[:max_findings]
    lines: list[str] = []
    for f in sample:
        loc = f"{f.file}:{f.line}" if f.file else "unknown"
        lines.append(f"  [{f.report_type}] {f.severity}/{f.confidence_level} @ {loc}: {f.title}")
    return "\n".join(lines) if lines else "  (no findings)"


def _build_finding_key_set(findings: list[NormalizedFinding]) -> set[tuple[str, str]]:
    """Build a set of (report_type, title) keys for anti-hallucination check."""
    return {(f.report_type, f.title) for f in findings}


def _validate_top_finding(
    raw: object,
    finding_keys: set[tuple[str, str]],
) -> TopFinding | None:
    """Parse and validate a raw top_finding dict.

    Returns None (and warns) if:
    - Not a dict
    - report_type not in _ALL_REPORT_TYPES (C1 guard)
    - severity not valid
    - title references a finding not in the input set (anti-hallucination)
    """
    if not isinstance(raw, dict):
        logger.warning("top_finding is not a dict — dropped")
        return None

    report_type = raw.get("report_type", "")
    if report_type not in _ALL_REPORT_TYPES:
        logger.warning(
            "top_finding.report_type=%r is not a valid ReportType — dropped "
            "(anti-hallucination C1 guard)",
            report_type,
        )
        return None

    severity = raw.get("severity", "")
    if severity not in _ALL_SEVERITIES:
        logger.warning("top_finding.severity=%r is not valid — dropped", severity)
        return None

    title = str(raw.get("title", ""))
    # Anti-hallucination: title must match a finding in the input set
    key = (report_type, title)
    if finding_keys and key not in finding_keys:
        logger.warning(
            "top_finding title=%r for %s does not match any input finding — "
            "dropped (S10 anti-hallucination guard)",
            title,
            report_type,
        )
        return None

    file_val = raw.get("file")
    line_val = raw.get("line")

    try:
        return TopFinding(
            report_type=report_type,
            severity=severity,
            file=str(file_val) if file_val is not None else None,
            line=int(line_val) if isinstance(line_val, int) else None,
            title=title,
            description=str(raw.get("description", "")),
        )
    except (ValueError, TypeError) as exc:
        logger.warning("top_finding failed validation: %s — dropped", exc)
        return None


def build_prompt_item_for_rationale(
    summaries: list[PrimitiveRiskSummary],
    gate_score: GateScore,
    all_findings: list[NormalizedFinding],
) -> PromptItem:
    """Build the PromptItem for the C6 rationale (skill-first mode).

    One item per run — id is "narrative:confidence-gate". Response format
    is JSON (rationale string + top_findings array) matching the schema
    expected by _validate_top_finding.

    Args:
        summaries: Per-primitive risk summaries from Stage B.
        gate_score: The computed gate score and recommendation.
        all_findings: All normalized findings for the user prompt.

    Returns:
        A single PromptItem for the rationale judgment.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    summaries_text = _build_summaries_text(summaries)
    findings_text = _build_findings_text(all_findings)

    user_msg = RATIONALE_USER_TEMPLATE.format(
        recommendation=gate_score.recommendation,
        score=gate_score.score,
        threshold_pass=gate_score.threshold_pass,
        threshold_review=gate_score.threshold_review,
        summaries_text=summaries_text,
        findings_text=findings_text,
    )

    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "rationale": {"type": "string"},
            "top_findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "report_type": {"type": "string"},
                        "severity": {"type": "string"},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["report_type", "severity", "title", "description"],
                },
            },
        },
        "required": ["rationale", "top_findings"],
    }
    return PromptItem(
        id="narrative:confidence-gate",
        system=RATIONALE_SYSTEM_PROMPT,
        user=user_msg,
        response_format=ResponseFormat(type="json", schema=schema),
        model_tier="default",
    )


def apply_rationale_judgment(
    response: dict[str, object],
    all_findings: list[NormalizedFinding],
) -> RationaleResult:
    """Apply a pre-computed rationale judgment with anti-hallucination guard.

    Runs the same _validate_top_finding guard as draft_rationale — invalid
    top_findings entries are dropped with a logged warning.

    Args:
        response: The model's JSON response dict from the judgments envelope.
        all_findings: Input findings for anti-hallucination key-set build.

    Returns:
        A RationaleResult with rationale and validated top_findings.
    """
    rationale = str(response.get("rationale", ""))
    finding_keys = _build_finding_key_set(all_findings)

    raw_top = response.get("top_findings", [])
    if not isinstance(raw_top, list):
        raw_top = []

    top_findings: list[TopFinding] = []
    for raw_finding in raw_top:
        validated = _validate_top_finding(raw_finding, finding_keys)
        if validated is not None:
            top_findings.append(validated)

    return RationaleResult(rationale=rationale, top_findings=top_findings)

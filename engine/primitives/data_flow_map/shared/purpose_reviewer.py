"""S60 /data-flow-map — Stage E: Purpose-limitation review (LLM).

One Opus call given spec text + flow graph summary → list[PurposeIssue].

Anti-hallucination guard (S10 lesson):
  - Drop any PurposeIssue whose `field` is not in detected PIISource names.
  - Log warning to stderr.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from primitives.data_flow_map.shared.models import FlowEdge, PIISource, PurposeIssue
from primitives.data_flow_map.shared.prompts import (
    STAGE_E_SYSTEM_PROMPT,
    build_stage_e_user_message,
)

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem

_VALID_SEVERITIES = {"low", "medium", "high", "critical"}


def _build_sources_summary(sources: list[PIISource]) -> str:
    """Build a text summary of detected PII sources for the LLM."""
    if not sources:
        return "(no PII sources detected)"
    lines = []
    for s in sources:
        lines.append(
            f"- {s.name!r}: {s.classification}/{s.category} [{s.severity}] at {s.file}:{s.line}"
        )
    return "\n".join(lines)


def _build_flows_summary(flows: list[FlowEdge]) -> str:
    """Build a text summary of detected flow edges for the LLM."""
    if not flows:
        return "(no flows detected)"
    lines = []
    for f in flows:
        transform = f" (via {f.transformation})" if f.transformation else ""
        lines.append(f"- {f.from_node} → {f.to_node}{transform}")
    return "\n".join(lines)


def _parse_purpose_issues_from_llm(
    raw_response: str,
    known_source_names: set[str],
) -> list[PurposeIssue]:
    """Parse and validate LLM JSON response into PurposeIssue objects.

    Anti-hallucination: drops any issue whose `field` is not in
    known_source_names. Logs warning to stderr (S10 lesson).
    """
    issues: list[PurposeIssue] = []

    # Strip markdown code fences if present
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        print(
            f"[data-flow-map] WARNING: Stage E LLM response is not valid JSON: {exc}",
            file=sys.stderr,
        )
        return []

    if not isinstance(data, list):
        print(
            "[data-flow-map] WARNING: Stage E LLM response is not a JSON array",
            file=sys.stderr,
        )
        return []

    for item in data:
        if not isinstance(item, dict):
            continue

        field = item.get("field", "")
        declared_purpose = item.get("declared_purpose")
        observed_use = item.get("observed_use", "")
        severity = item.get("severity", "medium")

        if not field:
            continue

        # Anti-hallucination guard: drop issues referencing unknown fields
        if field not in known_source_names:
            print(
                f"[data-flow-map] WARNING: Stage E dropped PurposeIssue referencing "
                f"unknown field {field!r} (not in detected PIISource names). "
                f"Known: {sorted(known_source_names)[:10]}",
                file=sys.stderr,
            )
            continue

        if not observed_use:
            continue

        # Coerce severity to valid value
        if severity not in _VALID_SEVERITIES:
            severity = "medium"

        try:
            issue = PurposeIssue(
                field=field,
                declared_purpose=declared_purpose,
                observed_use=observed_use,
                severity=severity,
            )
            issues.append(issue)
        except ValidationError as exc:
            print(
                f"[data-flow-map] WARNING: Stage E dropped malformed PurposeIssue: {exc}",
                file=sys.stderr,
            )

    return issues


# ---------------------------------------------------------------------------
# Skill-first helpers (extract→judge→continue protocol)
# ---------------------------------------------------------------------------


def build_prompt_item_for_purpose_review(
    sources: list[PIISource],
    flows: list[FlowEdge],
    spec_text: str | None,
) -> PromptItem:
    """Build a single PromptItem for Stage E purpose-limitation review.

    One item per run (batched over all sources and flows), id = "review:purpose-limitation".

    Args:
        sources: Detected PIISource objects from Stage A.
        flows: Flow edges from Stage B.
        spec_text: Optional spec/design document text.

    Returns:
        PromptItem with id "review:purpose-limitation".
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    sources_summary = _build_sources_summary(sources)
    flows_summary = _build_flows_summary(flows)
    user_msg = build_stage_e_user_message(
        spec_text=spec_text,
        sources_summary=sources_summary,
        flows_summary=flows_summary,
    )

    purpose_issue_schema: dict[str, object] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "field": {"type": "string"},
                "declared_purpose": {"type": ["string", "null"]},
                "observed_use": {"type": "string"},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            },
            "required": ["field", "observed_use", "severity"],
        },
    }

    return PromptItem(
        id="review:purpose-limitation",
        system=STAGE_E_SYSTEM_PROMPT,
        user=user_msg,
        response_format=ResponseFormat(type="json", schema=purpose_issue_schema),
    )


def apply_purpose_judgment(
    response: object,
    known_source_names: set[str],
) -> list[PurposeIssue]:
    """Apply in-session judgment for Stage E purpose-limitation review.

    Runs the same anti-hallucination guard as ``_parse_purpose_issues_from_llm``:
    any issue whose ``field`` is not in ``known_source_names`` is dropped with
    a stderr warning.

    Args:
        response: The model's response (expected to be a list of dicts, as
            deserialized from the judgment envelope).
        known_source_names: Set of known PIISource names for anti-hallucination guard.

    Returns:
        List of validated PurposeIssue objects.
    """
    import json as _json  # noqa: PLC0415

    return _parse_purpose_issues_from_llm(_json.dumps(response), known_source_names)

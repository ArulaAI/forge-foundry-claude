"""C8 /architecture-review — Stage F: issue prioritization.

Builds the Stage F ranking prompt and applies the model's in-session ranking
judgment, falling back to a deterministic severity-based sort on an invalid
response.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

from primitives.architecture_review.shared.models import (
    CyclicDependency,
    LayerViolation,
    SubjectiveIssue,
)
from primitives.architecture_review.shared.prompts import (
    STAGE_F_SYSTEM_PROMPT,
    build_stage_f_user_message,
)

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem

_SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

AnyIssue = LayerViolation | CyclicDependency | SubjectiveIssue


def _severity_of(issue: AnyIssue) -> int:
    if isinstance(issue, (LayerViolation, SubjectiveIssue)):
        return _SEVERITY_ORDER.get(issue.severity, 1)
    else:
        # CyclicDependency — no explicit severity, treat as high
        return _SEVERITY_ORDER["high"]


def _build_issues_summary(issues: list[AnyIssue]) -> str:
    """Build a textual summary of issues for the LLM."""
    lines: list[str] = []
    for i, issue in enumerate(issues):
        if isinstance(issue, LayerViolation):
            lines.append(
                f"{i}. [LayerViolation/{issue.severity}] "
                f"{issue.from_module} -> {issue.to_module} "
                f"(source: {issue.source})"
            )
        elif isinstance(issue, CyclicDependency):
            lines.append(
                f"{i}. [CyclicDependency/high] "
                f"size={issue.size} modules: {', '.join(issue.modules[:5])}"
            )
        elif isinstance(issue, SubjectiveIssue):
            mod_str = f" in {issue.module}" if issue.module else ""
            lines.append(
                f"{i}. [SubjectiveIssue/{issue.severity}/{issue.kind}]{mod_str}: "
                f"{issue.description[:100]}"
            )
    return "\n".join(lines)


def build_prompt_item_for_ranking(all_issues: list[AnyIssue]) -> PromptItem:
    """Build a PromptItem for the Stage F re-ranking call (skill-first mode).

    The caller includes this item in the round-2-prompts.json envelope and the
    SKILL.md body fills in the response.  The id is always ``"rank:issues"``.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    summary = _build_issues_summary(all_issues)
    user_msg = build_stage_f_user_message(summary)

    ranking_schema: dict[str, object] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "issue_index": {"type": "integer"},
                "priority_rank": {"type": "integer"},
                "rationale": {"type": "string"},
            },
            "required": ["issue_index", "priority_rank", "rationale"],
        },
    }

    return PromptItem(
        id="rank:issues",
        system=STAGE_F_SYSTEM_PROMPT,
        user=user_msg,
        response_format=ResponseFormat(type="json", schema=ranking_schema),
    )


def apply_ranking_judgment(
    response_array: list[object],
    all_issues: list[AnyIssue],
) -> list[AnyIssue]:
    """Apply a model's in-session ranking judgment (skill-first mode).

    *response_array* is the parsed JSON value from the judgments envelope.
    Delegates to ``_parse_llm_rankings`` so the same fail-soft guard runs on
    both paths.  Falls back to static sort on None (invalid response).
    """
    raw = json.dumps(response_array)
    rankings = _parse_llm_rankings(raw, len(all_issues))
    if rankings is None:
        print(
            "[architecture-review] WARNING: Stage F ranking judgment invalid, "
            "falling back to static sort.",
            file=sys.stderr,
        )

        def sort_key(item: AnyIssue) -> tuple[int, int]:
            sev = _severity_of(item)
            type_order = (
                2
                if isinstance(item, LayerViolation)
                else 1
                if isinstance(item, CyclicDependency)
                else 0
            )
            return (-sev, -type_order)

        return sorted(all_issues, key=sort_key)

    return [all_issues[i] for i in rankings]


def _parse_llm_rankings(raw: str, n: int) -> list[int] | None:
    """Parse LLM ranking response. Returns ordered list of 0-based indices, or None on error."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = [ln for ln in cleaned.split("\n") if not ln.startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        data: Any = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, list):
        return None

    try:
        # Sort by priority_rank ascending
        ranked = sorted(data, key=lambda x: x.get("priority_rank", 999))
        indices = [int(r["issue_index"]) for r in ranked if 0 <= int(r["issue_index"]) < n]
        if not indices:
            return None
        return indices
    except (KeyError, ValueError, TypeError):
        return None

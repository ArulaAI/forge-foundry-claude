"""C4 /code-review — Lens reviewer.

Builds per-file prompt items for the skill-first extract→judge→continue protocol
and applies the model's judgments back into validated issues.
Anti-hallucination: drops any returned issue whose `file` field does not
match the file being reviewed; logs warning to stderr.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from primitives._shared.llm_parsing import map_confidence as _map_confidence
from primitives._shared.llm_parsing import strip_json_fence as _strip_json_fence
from primitives.code_review.shared.context_loader import (
    compute_linked_controls,
    enrich_prompt_with_compliance,
)
from primitives.code_review.shared.models import CodeReviewIssue, Lens, Severity
from primitives.code_review.shared.prompts import BATCHED_LENS_PROMPT

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.code_review.shared.context_loader import (
        ComplianceContext,
        DataFlowContext,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_LENSES: frozenset[str] = frozenset(
    {"bug", "error_handling", "convention", "documentation", "security_smell", "test_quality"}
)
_VALID_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})


def _parse_issues_from_raw(
    raw: str,
    file_path: Path,
    lens: Lens | None,
    compliance_ctx: ComplianceContext | None = None,
    data_flow_ctx: DataFlowContext | None = None,
) -> list[CodeReviewIssue]:
    """Parse LLM JSON response into CodeReviewIssue list.

    Anti-hallucination: drop issues whose `file` does not match file_path.
    #425: attach ``linked_controls`` when both S70 and S60 contexts mark
    this file as scoped.

    Args:
        raw: Raw LLM response string.
        file_path: The file currently under review (ground truth).
        lens: If provided, overrides the lens on each issue (per-lens mode).
              If None, reads lens from each item (batched mode).
        compliance_ctx: Loaded ``.foundry/context/compliance.yaml`` (S70),
            or ``None`` if absent.
        data_flow_ctx: Loaded ``.foundry/context/data-flow.json`` (S60),
            or ``None`` if absent.

    Returns:
        List of validated CodeReviewIssue objects.
    """
    file_name = file_path.name
    file_str = str(file_path)

    try:
        cleaned = _strip_json_fence(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("[code-review] lens_reviewer: JSON parse error for %s: %s", file_path, exc)
        return []

    if not isinstance(data, list):
        logger.warning(
            "[code-review] lens_reviewer: expected JSON array, got %s for %s",
            type(data).__name__,
            file_path,
        )
        return []

    issues: list[CodeReviewIssue] = []

    for item in data:
        if not isinstance(item, dict):
            continue

        # Anti-hallucination: validate `file` field if present
        item_file = item.get("file")
        if item_file is not None and item_file not in (
            file_name,
            file_str,
            str(file_path.resolve()),
        ):
            logger.warning(
                "[code-review] anti-hallucination: dropping issue with file=%r (expected %r) in %s",
                item_file,
                file_str,
                file_path,
            )
            continue

        # Determine lens
        if lens is not None:
            item_lens: str = lens
        else:
            item_lens = item.get("lens", "")
            if item_lens not in _VALID_LENSES:
                logger.warning(
                    "[code-review] lens_reviewer: invalid lens %r in item; skipping", item_lens
                )
                continue

        # Validate severity
        severity_raw = item.get("severity", "medium")
        if severity_raw not in _VALID_SEVERITIES:
            severity_raw = "medium"

        # Validate confidence_score
        confidence_score_raw = item.get("confidence_score", 0.5)
        if not isinstance(confidence_score_raw, (int, float)):
            confidence_score_raw = 0.5
        confidence_score = float(max(0.0, min(1.0, confidence_score_raw)))

        # Validate line
        line_raw = item.get("line", 1)
        if not isinstance(line_raw, int) or line_raw < 1:
            line_raw = 1

        # end_line
        end_line_raw = item.get("end_line")
        if end_line_raw is not None and (
            not isinstance(end_line_raw, int) or end_line_raw < line_raw
        ):
            end_line_raw = None

        # cwe_ids
        cwe_ids_raw = item.get("cwe_ids", [])
        if not isinstance(cwe_ids_raw, list):
            cwe_ids_raw = []
        cwe_ids = [str(c) for c in cwe_ids_raw if isinstance(c, str)]

        linked_controls = compute_linked_controls(file_str, compliance_ctx, data_flow_ctx)

        try:
            issue = CodeReviewIssue(
                lens=cast("Lens", item_lens),
                file=file_str,
                line=line_raw,
                end_line=end_line_raw,
                severity=cast("Severity", severity_raw),
                confidence=_map_confidence(confidence_score),
                confidence_score=confidence_score,
                title=str(item.get("title", "(no title)")),
                description=str(item.get("description", "")),
                suggestion=item.get("suggestion"),
                cwe_ids=cwe_ids,
                linked_controls=linked_controls,
            )
            issues.append(issue)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "[code-review] lens_reviewer: skipping malformed item: %s: %s",
                type(exc).__name__,
                exc,
            )

    return issues


# ---------------------------------------------------------------------------
# Skill-first helpers (extract→judge→continue protocol)
# ---------------------------------------------------------------------------


def build_prompt_item_for_file(
    file: Path,
    source_code: str,
    lenses: list[Lens],  # noqa: ARG001 — batched mode always uses BATCHED_LENS_PROMPT (all lenses)
    spec_text: str | None,
    compliance_ctx: ComplianceContext | None = None,
    data_flow_ctx: DataFlowContext | None = None,
    *,
    code_path: Path | None = None,
) -> dict[str, Any]:
    """Build a single prompt item (dict) for one file in skill-first batched mode.

    The item id is ``"review:<relative_file_path>"`` when ``code_path`` is
    given; otherwise falls back to the absolute file string.

    Returns a plain dict matching the PromptItem schema so this module has no
    import-time dependency on ``primitives._shared.protocol`` (lazy import
    discipline).
    """
    spec_section = ""
    if spec_text:
        spec_section = f"\n\nSpec context (for relevance):\n{spec_text[:2000]}"

    user_text = (
        f"File: {file}\n"
        f"Language: {file.suffix}\n"
        f"{spec_section}\n\n"
        f"Source code:\n```\n{source_code}\n```\n\n"
        "Return the JSON array of findings."
    )

    system_prompt = enrich_prompt_with_compliance(
        BATCHED_LENS_PROMPT, compliance_ctx, data_flow_ctx
    )

    if code_path is not None:
        try:
            rel = file.relative_to(code_path)
            item_id = f"review:{rel}"
        except ValueError:
            item_id = f"review:{file}"
    else:
        item_id = f"review:{file}"

    finding_schema: dict[str, Any] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "lens": {
                    "type": "string",
                    "enum": sorted(_VALID_LENSES),
                },
                "severity": {
                    "type": "string",
                    "enum": sorted(_VALID_SEVERITIES),
                },
                "confidence_score": {"type": "number"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "suggestion": {"type": "string"},
            },
            "required": ["lens", "severity", "title", "description"],
        },
    }

    return {
        "id": item_id,
        "system": system_prompt,
        "user": user_text,
        "response_format": {"type": "json", "schema": finding_schema},
    }


def apply_lens_judgments_for_file(
    response_array: list[Any],
    file: Path,
    compliance_ctx: ComplianceContext | None = None,
    data_flow_ctx: DataFlowContext | None = None,
) -> list[CodeReviewIssue]:
    """Apply the model's judgment for a single file.

    Delegates to ``_parse_issues_from_raw`` (the shared anti-hallucination
    guard) after JSON-serialising the response array.

    Args:
        response_array: The parsed JSON array from the judgment item's response.
        file: The Path of the file that was reviewed (for anti-hallucination).
        compliance_ctx: Optional S70 context.
        data_flow_ctx: Optional S60 context.

    Returns:
        List of validated CodeReviewIssue objects.
    """
    raw = json.dumps(response_array)
    return _parse_issues_from_raw(
        raw,
        file,
        lens=None,
        compliance_ctx=compliance_ctx,
        data_flow_ctx=data_flow_ctx,
    )

"""S11 /security-review — Lens reviewer (Stage B).

Builds per-file prompt items for the skill-first extract→judge→continue protocol
and applies the model's judgments back into validated findings.

Anti-hallucination (S10 lesson):
  1. SecurityFinding.file validated against reviewed-files set; drop + log on mismatch.
  2. compliance_refs validated against compliance.yaml when present; drop unknown refs + log.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

from pydantic import ValidationError

from primitives._shared.llm_parsing import map_confidence as _map_confidence
from primitives._shared.llm_parsing import strip_json_fence as _strip_json_fence
from primitives.security_review.shared.models import (
    OWASPCategory,
    SecurityFinding,
    SecurityLens,
    Severity,
)
from primitives.security_review.shared.prompts import BATCHED_SECURITY_LENS_PROMPT

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_LENSES: frozenset[str] = frozenset(
    {
        "authn",
        "authz",
        "crypto",
        "input_handling",
        "data_exposure",
        "secrets_mgmt",
        "owasp_top10",
    }
)
_VALID_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})
_VALID_OWASP: frozenset[str] = frozenset(
    {"A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10"}
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_compliance_refs(
    refs: list[str],
    valid_refs: set[str] | None,
    file_path: Path,
    line: int,
) -> list[str]:
    """Validate compliance_refs against the known compliance.yaml keys.

    Anti-hallucination 2: drop unknown refs with warning.

    Args:
        refs: compliance_refs from LLM output.
        valid_refs: Set of valid ref strings from compliance.yaml, or None (no validation).
        file_path: For logging context.
        line: For logging context.

    Returns:
        Filtered list of valid refs.
    """
    if valid_refs is None:
        return refs

    validated: list[str] = []
    for ref in refs:
        if ref in valid_refs:
            validated.append(ref)
        else:
            logger.warning(
                "[security-review] anti-hallucination: dropping unknown compliance_ref "
                "%r at %s:%s (not in compliance.yaml)",
                ref,
                file_path,
                line,
            )
    return validated


def _parse_findings_from_raw(
    raw: str,
    file_path: Path,
    lens: SecurityLens | None,
    valid_compliance_refs: set[str] | None,
) -> list[SecurityFinding]:
    """Parse LLM JSON response into SecurityFinding list.

    Anti-hallucination 1: drop findings whose `file` does not match file_path.
    Anti-hallucination 2: validate compliance_refs against compliance.yaml.

    Args:
        raw: Raw LLM response string.
        file_path: The file currently under review (ground truth).
        lens: If provided, overrides lens on each finding (per-lens mode).
              If None, reads lens from each item (batched mode).
        valid_compliance_refs: Known valid compliance ref strings, or None.

    Returns:
        List of validated SecurityFinding objects.
    """
    file_name = file_path.name
    file_str = str(file_path)

    try:
        cleaned = _strip_json_fence(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "[security-review] lens_reviewer: JSON parse error for %s: %s", file_path, exc
        )
        return []

    if not isinstance(data, list):
        logger.warning(
            "[security-review] lens_reviewer: expected JSON array, got %s for %s",
            type(data).__name__,
            file_path,
        )
        return []

    findings: list[SecurityFinding] = []

    for item in data:
        if not isinstance(item, dict):
            continue

        # --- Anti-hallucination 1: validate `file` field if present ---
        item_file = item.get("file")
        if item_file is not None and item_file not in (
            file_name,
            file_str,
            str(file_path.resolve()),
        ):
            logger.warning(
                "[security-review] anti-hallucination: dropping finding with "
                "file=%r (expected %r) in %s",
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
                    "[security-review] lens_reviewer: invalid lens %r; skipping", item_lens
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

        # owasp_category — validate against Literal (C1 lesson)
        owasp_raw = item.get("owasp_category")
        if owasp_raw is not None and owasp_raw not in _VALID_OWASP:
            logger.warning(
                "[security-review] lens_reviewer: invalid owasp_category %r; clearing to None",
                owasp_raw,
            )
            owasp_raw = None

        # cwe_ids
        cwe_ids_raw = item.get("cwe_ids", [])
        if not isinstance(cwe_ids_raw, list):
            cwe_ids_raw = []
        cwe_ids = [str(c) for c in cwe_ids_raw if isinstance(c, str)]

        # compliance_refs — collect then validate (anti-hallucination 2)
        refs_raw = item.get("compliance_refs", [])
        if not isinstance(refs_raw, list):
            refs_raw = []
        refs = [str(r) for r in refs_raw if isinstance(r, str)]
        refs = _validate_compliance_refs(refs, valid_compliance_refs, file_path, line_raw)

        try:
            finding = SecurityFinding(
                lens=cast("SecurityLens", item_lens),
                file=file_str,
                line=line_raw,
                end_line=end_line_raw,
                severity=cast("Severity", severity_raw),
                confidence=_map_confidence(confidence_score),
                confidence_score=confidence_score,
                title=str(item.get("title", "(no title)")),
                description=str(item.get("description", "")),
                suggestion=item.get("suggestion"),
                owasp_category=cast("OWASPCategory | None", owasp_raw),
                cwe_ids=cwe_ids,
                compliance_refs=refs,
            )
            findings.append(finding)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "[security-review] lens_reviewer: skipping malformed item: %s: %s",
                type(exc).__name__,
                exc,
            )

    return findings


# ---------------------------------------------------------------------------
# Skill-first helpers (extract→judge→continue protocol)
# ---------------------------------------------------------------------------


def build_prompt_item_for_file(
    file: Path,
    source_code: str,
    lenses: list[SecurityLens],  # noqa: ARG001 — batched mode always uses BATCHED_SECURITY_LENS_PROMPT
    spec_text: str | None,
    valid_compliance_refs: set[str] | None = None,  # noqa: ARG001 — reserved for future enrichment
    *,
    code_path: Path | None = None,
) -> dict[str, Any]:
    """Build a single prompt item (dict) for one file in skill-first batched mode.

    The item id is ``"security:<relative_file_path>"`` when ``code_path`` is
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
        "Return the JSON array of security findings."
    )

    if code_path is not None:
        try:
            rel = file.relative_to(code_path)
            item_id = f"security:{rel}"
        except ValueError:
            item_id = f"security:{file}"
    else:
        item_id = f"security:{file}"

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
                "owasp_category": {
                    "type": "string",
                    "enum": sorted(_VALID_OWASP),
                },
                "cwe_ids": {"type": "array", "items": {"type": "string"}},
                "compliance_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["lens", "severity", "title", "description"],
        },
    }

    return {
        "id": item_id,
        "system": BATCHED_SECURITY_LENS_PROMPT,
        "user": user_text,
        "response_format": {"type": "json", "schema": finding_schema},
    }


def apply_lens_judgments_for_file(
    response_array: list[Any],
    file: Path,
    valid_compliance_refs: set[str] | None = None,
) -> list[SecurityFinding]:
    """Apply the model's judgment for a single file.

    Delegates to ``_parse_findings_from_raw`` (the shared anti-hallucination
    guard) after JSON-serialising the response array.

    Args:
        response_array: The parsed JSON array from the judgment item's response.
        file: The Path of the file that was reviewed (for anti-hallucination).
        valid_compliance_refs: Known compliance ref strings, or None.

    Returns:
        List of validated SecurityFinding objects.
    """
    raw = json.dumps(response_array)
    return _parse_findings_from_raw(
        raw, file, lens=None, valid_compliance_refs=valid_compliance_refs
    )

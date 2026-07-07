"""S40 /api-contract-review — Lens reviewer (Stage B).

Builds the batched lens prompt for a contract file and applies the model's
judgment array back through the anti-hallucination validators.

Three anti-hallucination validators (S10/S20 lesson, deeper):
  1. `file` validated against reviewed-files set; DROP + warning on mismatch.
  2. `endpoint` validated against parsed Endpoint list; CLEAR (set to None) + warning
     if not in known endpoints; finding retained (S20 lesson).
  3. `rfc_ref` validated against regex ^RFC\\s?\\d{3,5}$ or ^OWASP\\s.*$;
     CLEAR (set to None) + warning if malformed; finding retained (S20 lesson).

Skill-first protocol helpers:
  - ``build_prompt_item_for_contract`` — build a PromptItem for a contract file
    (batched mode, all lenses in one prompt).
  - ``apply_lens_judgments_for_contract`` — apply the model's response array,
    running all three anti-hallucination validators.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from primitives._shared.llm_parsing import map_confidence as _map_confidence
from primitives._shared.llm_parsing import strip_json_fence as _strip_json_fence
from primitives.api_contract_review.shared.models import (
    APIFinding,
    APILens,
    Endpoint,
    Severity,
)
from primitives.api_contract_review.shared.prompts import BATCHED_LENS_PROMPT

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.protocol import PromptItem

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_LENSES: frozenset[str] = frozenset(
    {
        "versioning",
        "error_shapes",
        "auth_spec",
        "idempotency",
        "backward_compatibility",
        "pagination",
        "rate_limiting",
    }
)
_VALID_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})

# Anti-hallucination 3: rfc_ref regex — must match RFC NNNNN or OWASP <text>
_RFC_REF_RE = re.compile(r"^RFC\s?\d{3,5}$|^OWASP\s.*$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_endpoint_set(endpoints: list[Endpoint]) -> set[str]:
    """Build a set of valid endpoint descriptors from parsed Endpoint objects."""
    valid: set[str] = set()
    for ep in endpoints:
        # e.g., "GET /users" or "publish user.created" or "Query.getUser"
        descriptor = f"{ep.method_or_op} {ep.path}"
        valid.add(descriptor)
        valid.add(ep.path)
        valid.add(ep.method_or_op)
    return valid


def _parse_findings_from_raw(
    raw: str,
    file_path: Path,
    lens: APILens | None,
    reviewed_file_set: set[str],
    endpoint_set: set[str],
) -> list[APIFinding]:
    """Parse LLM JSON response into APIFinding list.

    Three anti-hallucination validators (S10/S20 lesson):
    1. `file` must be in reviewed_file_set (DROP + warn on mismatch).
    2. `endpoint` must be in endpoint_set (CLEAR + warn; finding retained).
    3. `rfc_ref` must match RFC regex (CLEAR + warn; finding retained).

    Args:
        raw: Raw LLM response string.
        file_path: The file currently under review (ground truth).
        lens: If provided, overrides lens on each finding (per-lens mode).
              If None, reads lens from each item (batched mode).
        reviewed_file_set: Set of valid reviewed file paths/names (anti-hallucination 1).
        endpoint_set: Set of valid endpoint descriptors (anti-hallucination 2).

    Returns:
        List of validated APIFinding objects.
    """
    file_str = str(file_path)

    try:
        cleaned = _strip_json_fence(raw)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "[api-contract-review] lens_reviewer: JSON parse error for %s: %s", file_path, exc
        )
        return []

    if not isinstance(data, list):
        logger.warning(
            "[api-contract-review] lens_reviewer: expected JSON array, got %s for %s",
            type(data).__name__,
            file_path,
        )
        return []

    findings: list[APIFinding] = []

    for item in data:
        if not isinstance(item, dict):
            continue

        # --- Anti-hallucination 1: validate `file` — DROP on mismatch ---
        item_file = item.get("file")
        if item_file is not None and item_file not in reviewed_file_set:
            logger.warning(
                "[api-contract-review] anti-hallucination (file): dropping finding with "
                "file=%r (not in reviewed files set) for %s",
                item_file,
                file_path,
            )
            continue

        # --- Anti-hallucination 2: validate `endpoint` — CLEAR if unknown (finding retained) ---
        item_endpoint = item.get("endpoint")
        if (
            item_endpoint is not None
            and item_endpoint != ""
            and endpoint_set
            and item_endpoint not in endpoint_set
        ):
            logger.warning(
                "[api-contract-review] anti-hallucination (endpoint): clearing "
                "endpoint=%r on finding (not in parsed endpoints for %s); "
                "finding retained without endpoint",
                item_endpoint,
                file_path,
            )
            item_endpoint = None

        # --- Anti-hallucination 3: validate `rfc_ref` — CLEAR if malformed (retained) ---
        item_rfc_ref = item.get("rfc_ref")
        if item_rfc_ref is not None and item_rfc_ref != "":
            if not _RFC_REF_RE.match(str(item_rfc_ref)):
                logger.warning(
                    "[api-contract-review] anti-hallucination (rfc_ref): clearing "
                    "rfc_ref=%r on finding (does not match "
                    r"^RFC\s?\d{3,5}$ or ^OWASP\s.*$); finding retained without rfc_ref",
                    item_rfc_ref,
                )
                item_rfc_ref = None
            else:
                item_rfc_ref = str(item_rfc_ref)

        # Determine lens
        if lens is not None:
            item_lens: str = lens
        else:
            item_lens = item.get("lens", "")
            if item_lens not in _VALID_LENSES:
                logger.warning(
                    "[api-contract-review] lens_reviewer: invalid lens %r; skipping", item_lens
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

        # breaking
        breaking_raw = item.get("breaking", False)
        breaking = bool(breaking_raw)

        try:
            finding = APIFinding(
                lens=cast("APILens", item_lens),
                file=file_str,
                line=line_raw,
                endpoint=item_endpoint if item_endpoint else None,
                severity=cast("Severity", severity_raw),
                confidence=_map_confidence(confidence_score),
                confidence_score=confidence_score,
                title=str(item.get("title", "(no title)")),
                description=str(item.get("description", "")),
                suggestion=item.get("suggestion"),
                rfc_ref=item_rfc_ref,
                breaking=breaking,
            )
            findings.append(finding)
        except (ValidationError, TypeError, ValueError) as exc:
            logger.warning(
                "[api-contract-review] lens_reviewer: skipping malformed item: %s: %s",
                type(exc).__name__,
                exc,
            )

    return findings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Skill-first protocol helpers
# ---------------------------------------------------------------------------


def build_prompt_item_for_contract(
    file: Path,
    contract_content: str,
    lenses: list[APILens],  # noqa: ARG001 — kept for API symmetry; batched prompt covers all lenses
    context: str | None,
    endpoints: list[Endpoint],  # noqa: ARG001 — guard runs at continue time, not here
    reviewed_file_set: set[str],  # noqa: ARG001 — kept for API symmetry
) -> PromptItem:
    """Build a PromptItem for a single contract file (batched mode, all lenses).

    The item id is ``"contract:<relative_or_base_file_name>"``.
    The system prompt is ``BATCHED_LENS_PROMPT``; the user prompt mirrors
    ``review_lens`` batched mode exactly, so the model sees the same context
    as in CI mode.

    Args:
        file: Path to the contract file.
        contract_content: Text content of the file.
        lenses: Lenses to apply (unused in batched prompt but preserved for future).
        context: Optional context string (e.g., baseline diff summary).
        endpoints: Parsed endpoints from this file (unused — anti-hallucination
            guard runs in ``apply_lens_judgments_for_contract`` at continue time).
        reviewed_file_set: Not used in prompt construction; kept for signature symmetry.

    Returns:
        A ``PromptItem`` (from ``primitives._shared.protocol``).
    """
    # Lazy import to avoid circular deps and keep cold-start cheap.
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    context_section = ""
    if context:
        context_section = f"\n\nContext:\n{context}"

    user_prefix = (
        f"Contract file: {file}\n"
        f"File extension: {file.suffix}"
        f"{context_section}\n\n"
        f"Contract content:\n```\n{contract_content}\n```\n\n"
        "Return the JSON array of API contract review findings."
    )

    # Response schema: array of APIFinding-shaped objects.
    response_schema: dict[str, object] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "lens": {"type": "string"},
                "file": {"type": "string"},
                "line": {"type": "integer"},
                "endpoint": {"type": ["string", "null"]},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "confidence_score": {"type": "number"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "suggestion": {"type": ["string", "null"]},
                "rfc_ref": {"type": ["string", "null"]},
                "breaking": {"type": "boolean"},
            },
            "required": ["lens", "file", "severity", "title", "description"],
        },
    }

    return PromptItem(
        id=f"contract:{file.name}",
        system=BATCHED_LENS_PROMPT,
        user=user_prefix,
        response_format=ResponseFormat(type="json", schema=response_schema),
    )


def apply_lens_judgments_for_contract(
    response_array: list[object],
    file: Path,
    reviewed_file_set: set[str],
    endpoint_set: set[str],
) -> list[APIFinding]:
    """Apply the model's JSON response array for a contract file.

    Delegates to ``_parse_findings_from_raw`` to apply all three
    anti-hallucination validators identically to the harness path.

    Args:
        response_array: The model's response (already parsed as a list).
        file: The contract file path (used for anti-hallucination 1).
        reviewed_file_set: Set of valid file paths/names (anti-hallucination 1).
        endpoint_set: Set of valid endpoint descriptors (anti-hallucination 2).

    Returns:
        List of validated ``APIFinding`` objects.
    """
    return _parse_findings_from_raw(
        json.dumps(response_array),
        file,
        lens=None,
        reviewed_file_set=reviewed_file_set,
        endpoint_set=endpoint_set,
    )

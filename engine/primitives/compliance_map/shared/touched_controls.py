"""S70 /compliance-map — Stage C: touched control identification.

Skill-first protocol:
  - ``build_prompt_items_for_controls`` — builds one PromptItem per
    (framework, family) group for ``--extract-only``.
  - ``apply_controls_judgments`` — applies the in-session judgment dict to
    produce ``list[ControlMatch]`` via the ``_parse_touched_response`` guard.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import TYPE_CHECKING

from primitives.compliance_map.shared.models import Control, ControlMatch
from primitives.compliance_map.shared.prompts import TOUCHED_CONTROLS_SYSTEM_PROMPT

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem
    from primitives.compliance_map.shared.catalog_loader import ControlCatalog


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class LLMResponseError(Exception):
    """Raised when LLM returns un-parseable JSON for touched-control identification."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _group_by_family(controls: list[Control]) -> dict[str, list[Control]]:
    """Group controls by family for batched LLM calls."""
    groups: dict[str, list[Control]] = defaultdict(list)
    for c in controls:
        groups[c.family].append(c)
    return dict(groups)


def _build_user_message(spec_text: str, controls: list[Control], family: str) -> str:
    """Build the user message for a Stage C LLM call."""
    controls_json = json.dumps(
        [
            {
                "id": c.id,
                "title": c.title,
                "family": c.family,
                "severity": c.severity,
                "evidence_hints": c.evidence_hints,
            }
            for c in controls
        ],
        indent=2,
    )
    return (
        f"# Feature Specification\n\n"
        f"{spec_text[:8000]}\n\n"
        f"---\n\n"
        f"# Controls to assess (framework: {controls[0].framework}, family: {family})\n\n"
        f"```json\n{controls_json}\n```\n\n"
        f"Identify which of these controls the specification TOUCHES. Return a JSON array."
    )


def _parse_touched_response(
    raw: str,
    controls_by_id: dict[str, Control],
    framework: str,
    family: str,
) -> list[ControlMatch]:
    """Parse LLM response into ControlMatch objects.

    Raises:
        LLMResponseError: if response is not valid JSON or has missing fields.
    """
    stripped = raw.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        stripped = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LLMResponseError(
            f"LLM returned non-JSON for {framework}/{family}: {exc}\nRaw: {raw[:300]}"
        ) from exc

    if not isinstance(data, list):
        raise LLMResponseError(
            f"Expected JSON array for {framework}/{family}, got {type(data).__name__}"
        )

    matches: list[ControlMatch] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise LLMResponseError(
                f"Item {i} in {framework}/{family} response is not a dict: {item!r}"
            )
        control_id = item.get("control_id", "")
        touched = item.get("touched", False)

        if not touched:
            continue  # abstain

        control = controls_by_id.get(str(control_id))
        if control is None:
            print(
                f"WARNING: LLM returned unknown control_id {control_id!r} "
                f"for {framework}/{family} — skipping",
                file=sys.stderr,
            )
            continue

        matches.append(
            ControlMatch(
                control=control,
                status="unaddressed",  # default; Stage D/E will update
                evidence=[],
                gap_description=None,
                remediation_owner="engineering",
            )
        )

    return matches


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Skill-first helpers
# ---------------------------------------------------------------------------

_TOUCHED_CONTROLS_RESPONSE_SCHEMA: dict[str, object] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "control_id": {"type": "string"},
            "touched": {"type": "boolean"},
            "rationale": {"type": "string"},
        },
        "required": ["control_id", "touched", "rationale"],
    },
}


def build_prompt_items_for_controls(
    catalog: ControlCatalog,
    spec_text: str,
) -> list[PromptItem]:
    """Build one PromptItem per (framework, family) group for --extract-only.

    The item id is ``"controls:<framework>/<family>"`` — the same key space
    consumed by ``apply_controls_judgments``.

    Args:
        catalog: Loaded ControlCatalog for a single framework.
        spec_text: Full text of the feature specification.

    Returns:
        List of PromptItem objects ready to be included in a PromptEnvelope.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    families = _group_by_family(catalog.controls)
    items: list[PromptItem] = []
    for family in sorted(families):
        controls = families[family]
        user = _build_user_message(spec_text, controls, family)
        item_id = f"controls:{catalog.framework}/{family}"
        items.append(
            PromptItem(
                id=item_id,
                system=TOUCHED_CONTROLS_SYSTEM_PROMPT,
                user=user,
                response_format=ResponseFormat(
                    type="json",
                    schema=_TOUCHED_CONTROLS_RESPONSE_SCHEMA,
                ),
            )
        )
    return items


def apply_controls_judgments(
    catalog: ControlCatalog,
    judgment_map: dict[str, object],
) -> list[ControlMatch]:
    """Apply in-session judgments to produce ControlMatch objects.

    Mirrors the anti-hallucination logic of ``_parse_touched_response``: unknown
    control IDs are logged and skipped; controls absent from the judgment map are
    treated as untouched (conservative / safe-fail).

    Args:
        catalog: The ControlCatalog whose families were prompted.
        judgment_map: Dict mapping item id → raw judgment value (a list of dicts
            matching the response schema, or a JSON string of same).

    Returns:
        List of ControlMatch objects for controls the model marked as touched.
    """
    families = _group_by_family(catalog.controls)
    controls_by_id = {c.id: c for c in catalog.controls}
    all_matches: list[ControlMatch] = []

    for family in sorted(families):
        item_id = f"controls:{catalog.framework}/{family}"
        raw_judgment = judgment_map.get(item_id)
        if raw_judgment is None:
            # Item not judged — treat whole family as untouched (safe-fail).
            print(
                f"[compliance-map] WARNING: no judgment for {item_id} — treating as untouched",
                file=sys.stderr,
            )
            continue

        # Accept either a pre-parsed list or a JSON string (both formats occur in tests).
        if isinstance(raw_judgment, list):
            raw_str = json.dumps(raw_judgment)
        elif isinstance(raw_judgment, str):
            raw_str = raw_judgment
        else:
            raw_str = json.dumps(raw_judgment)

        try:
            matches = _parse_touched_response(
                raw_str,
                controls_by_id=controls_by_id,
                framework=catalog.framework,
                family=family,
            )
        except LLMResponseError as exc:
            print(
                f"[compliance-map] WARNING: judgment parse error for {item_id}: {exc} — skipping",
                file=sys.stderr,
            )
            continue

        all_matches.extend(matches)

    return all_matches

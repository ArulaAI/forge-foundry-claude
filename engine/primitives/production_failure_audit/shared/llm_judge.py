"""LLM judge for FM6 / FM7 / FM10 — opt-in via .foundry/failure-modes.yaml.

Bounded by an optional LlmCallCounter; raises BudgetExceededError when
exceeded.

Ships in v0.5.0 as standalone helpers. NOT yet wired into
audit_production_path() dispatch — that's v0.6.0 (mirrors C10's decision
per design spec §13).

Skill-first helpers (build_prompt_items_for_fm6/fm7/fm10_candidates,
apply_fm6/fm7/fm10_judgment) implement the extract→judge→continue protocol
for when config.requires_llm() is True. The prompts are only emitted when
LLM mode is actually configured — by default (mode=regex) no prompts are
produced and the CLI exits 0.

Refs: GH issue #76
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem
    from primitives.production_failure_audit.shared.findings import Finding


class BudgetExceededError(RuntimeError):
    """Raised when the LLM call count exceeds the configured budget."""


FM6_SYSTEM_PROMPT = (
    "You are a stack-version compatibility expert. Given a code snippet and the "
    "project's declared stack version, verify whether the snippet uses idioms "
    "appropriate for that version. Respond JSON: {verdict: 'supported' | 'refuted', "
    "reasoning: '...'}"
)

FM7_SYSTEM_PROMPT = (
    "You are a domain-fit security reviewer. Given a code snippet and the project's "
    "domain (e.g., 'payments', 'healthcare'), verify whether the snippet's "
    "generic-vs-specific choice is appropriate. Respond JSON: {verdict: ..., reasoning: ...}"
)

FM10_SYSTEM_PROMPT = (
    "You are an architecture reviewer evaluating whether an import respects established "
    "layer boundaries. Respond JSON: {verdict: 'supported' | 'refuted', reasoning: '...'}"
)


class LlmCallCounter:
    """Tracks LLM calls per audit; raises BudgetExceededError when over budget.

    Usage:
        counter = LlmCallCounter(max_calls=20)
        for candidate in candidates:
            counter.record_call()
            judge_fm6_candidate(candidate, ..., counter=counter)
    """

    def __init__(self, max_calls: int) -> None:
        self._max = max_calls
        self._count = 0

    @property
    def calls_made(self) -> int:
        return self._count

    def record_call(self) -> None:
        """Increment the counter. Raise BudgetExceededError if it would exceed max."""
        if self._count >= self._max:
            raise BudgetExceededError(
                f"Production-failure LLM budget exceeded: {self._count} calls made, max={self._max}"
            )
        self._count += 1


def _parse_verdict(text: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("verdict") not in ("supported", "refuted"):
        return None
    return data


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (extract→judge→continue)
# ---------------------------------------------------------------------------

_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "refuted"]},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "reasoning"],
}


def _build_fm6_user(candidate: Finding, stack_name: str, stack_version: str) -> str:
    return (
        f"File: {candidate.file}\nStack: {stack_name} version {stack_version}\n"
        f"Snippet (line {candidate.line}):\n{candidate.snippet}\n\n"
        f"Regex flagged: {candidate.message}\nConfirm or refute."
    )


def _build_fm7_user(candidate: Finding, project_domain: str) -> str:
    return (
        f"File: {candidate.file}\nProject domain: {project_domain}\n"
        f"Snippet (line {candidate.line}):\n{candidate.snippet}\n\n"
        f"Regex flagged: {candidate.message}\nConfirm or refute."
    )


def _build_fm10_user(candidate: Finding) -> str:
    return (
        f"File: {candidate.file}\nSnippet (line {candidate.line}):\n{candidate.snippet}\n\n"
        f"Regex flagged: {candidate.message}\nConfirm or refute."
    )


def build_prompt_items_for_fm6_candidates(
    candidates: list[Finding],
    stack_name: str,
    stack_version: str,
) -> list[PromptItem]:
    """Build one PromptItem per FM6 candidate for skill-first mode.

    Each item id is ``judge:fm6:<index>`` (0-based). Uses ``model_tier="deep"``
    matching the existing ``model_logical="opus"`` calls.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    items: list[PromptItem] = []
    for idx, candidate in enumerate(candidates):
        user = _build_fm6_user(candidate, stack_name, stack_version)
        items.append(
            PromptItem(
                id=f"judge:fm6:{idx}",
                system=FM6_SYSTEM_PROMPT,
                user=user,
                response_format=ResponseFormat(type="json", schema=_VERDICT_SCHEMA),
                model_tier="deep",
            )
        )
    return items


def build_prompt_items_for_fm7_candidates(
    candidates: list[Finding],
    project_domain: str,
) -> list[PromptItem]:
    """Build one PromptItem per FM7 candidate for skill-first mode.

    Each item id is ``judge:fm7:<index>`` (0-based). Uses ``model_tier="deep"``.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    items: list[PromptItem] = []
    for idx, candidate in enumerate(candidates):
        user = _build_fm7_user(candidate, project_domain)
        items.append(
            PromptItem(
                id=f"judge:fm7:{idx}",
                system=FM7_SYSTEM_PROMPT,
                user=user,
                response_format=ResponseFormat(type="json", schema=_VERDICT_SCHEMA),
                model_tier="deep",
            )
        )
    return items


def build_prompt_items_for_fm10_candidates(
    candidates: list[Finding],
) -> list[PromptItem]:
    """Build one PromptItem per FM10 candidate for skill-first mode.

    Each item id is ``judge:fm10:<index>`` (0-based). Uses ``model_tier="deep"``.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    items: list[PromptItem] = []
    for idx, candidate in enumerate(candidates):
        user = _build_fm10_user(candidate)
        items.append(
            PromptItem(
                id=f"judge:fm10:{idx}",
                system=FM10_SYSTEM_PROMPT,
                user=user,
                response_format=ResponseFormat(type="json", schema=_VERDICT_SCHEMA),
                model_tier="deep",
            )
        )
    return items


def apply_fm6_judgment(
    candidate: Finding,
    response: dict[str, Any],
) -> Finding | None:
    """Apply a skill-first FM6 judgment. Returns rewritten Finding or None."""
    parsed = _parse_verdict(json.dumps(response))
    if parsed is None or parsed["verdict"] == "refuted":
        return None
    return replace(
        candidate,
        message=f"{candidate.message} | LLM-confirmed: {str(parsed.get('reasoning', '')).strip()}",
        rule_id=f"{candidate.rule_id}.llm-confirmed",
    )


def apply_fm7_judgment(
    candidate: Finding,
    response: dict[str, Any],
) -> Finding | None:
    """Apply a skill-first FM7 judgment. Returns rewritten Finding or None."""
    parsed = _parse_verdict(json.dumps(response))
    if parsed is None or parsed["verdict"] == "refuted":
        return None
    return replace(
        candidate,
        message=f"{candidate.message} | LLM-confirmed: {str(parsed.get('reasoning', '')).strip()}",
        rule_id=f"{candidate.rule_id}.llm-confirmed",
    )


def apply_fm10_judgment(
    candidate: Finding,
    response: dict[str, Any],
) -> Finding | None:
    """Apply a skill-first FM10 judgment. Returns rewritten Finding or None."""
    parsed = _parse_verdict(json.dumps(response))
    if parsed is None or parsed["verdict"] == "refuted":
        return None
    return replace(
        candidate,
        message=f"{candidate.message} | LLM-confirmed: {str(parsed.get('reasoning', '')).strip()}",
        rule_id=f"{candidate.rule_id}.llm-confirmed",
    )


def serialize_finding(f: Finding) -> dict[str, Any]:
    """Serialize a Finding for intermediate storage (JSON-compatible)."""
    return asdict(f)


def deserialize_finding(d: dict[str, Any]) -> Finding:
    """Deserialize a Finding from intermediate storage."""
    from primitives.production_failure_audit.shared.findings import Finding as F  # noqa: PLC0415

    return F(**d)

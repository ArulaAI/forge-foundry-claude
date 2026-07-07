"""LLM-judged path for FM3 / FM4 — opt-in via .foundry/failure-modes.yaml.

Bounded by config.llm.max_calls_per_audit; raises BudgetExceededError when
exceeded.

Foundry-native module — prowess U1 deferred LLM-judged work to Plan 2 but
that plan delivered other primitives instead, leaving FM3/FM4 regex-only.

Skill-first helpers (build_prompt_items_for_fm3/fm4_candidates,
apply_fm3/fm4_judgment) implement the extract→judge→continue protocol so
the SKILL.md body judges candidates in-session.
"""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from primitives.failure_mode_audit.shared.findings import Finding

if TYPE_CHECKING:
    from primitives._shared.protocol import PromptItem


class BudgetExceededError(RuntimeError):
    """Raised when the LLM call count exceeds AuditConfig.llm.max_calls_per_audit."""


FM3_SYSTEM_PROMPT = (
    "You are a static-analysis expert detecting outdated stack idioms.\n"
    "Given a code snippet and the project's stack version, report whether the\n"
    "snippet uses idioms appropriate for that version.\n"
    'Respond in JSON: {"verdict": "supported" | "refuted", "reasoning": "..."}'
)

FM4_SYSTEM_PROMPT = (
    "You are a code reviewer detecting unsupported claims in code comments.\n"
    "Given a function body and its comments, report whether comments make claims\n"
    "the implementation clearly delivers, or whether claims are unsupported.\n"
    'Respond in JSON: {"verdict": "supported" | "refuted" | "new", '
    '"reasoning": "...", "claim_line": int, "severity": "low" | "medium" | "high"}'
)


def _build_fm3_user(candidate: Finding, stack_name: str, stack_version: str) -> str:
    return (
        f"File: {candidate.file}\n"
        f"Stack: {stack_name} version {stack_version}\n"
        f"Snippet (line {candidate.line}):\n"
        f"{candidate.snippet}\n\n"
        f"Regex flagged this as: {candidate.message}\n"
        "Confirm or refute."
    )


def _parse_fm3_response(text: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("verdict") not in ("supported", "refuted"):
        return None
    return data


def _build_fm4_user(
    file: str,
    start_line: int,
    end_line: int,
    function_body: str,
    regex_finding: Finding | None,
) -> str:
    regex_part = "None" if regex_finding is None else f"Regex finding: {regex_finding.message}"
    return (
        f"File: {file}\nFunction (lines {start_line}-{end_line}):\n{function_body}\n\n{regex_part}"
    )


def _parse_fm4_response(text: str) -> dict[str, object] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in ("supported", "refuted", "new"):
        return None
    if verdict == "new":
        if not isinstance(data.get("claim_line"), int):
            return None
        if data.get("severity") not in ("low", "medium", "high"):
            return None
    return data


class LlmCallCounter:
    """Tracks LLM calls per audit; raises BudgetExceededError when over budget.

    Usage:
        counter = LlmCallCounter(max_calls=config.llm.max_calls_per_audit)
        for candidate in candidates:
            counter.record_call()
            judge_fm3_candidate(...)
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
                f"LLM call budget exceeded: {self._count} calls already made, "
                f"max_calls_per_audit = {self._max}. Narrow the audit path or "
                "raise the cap in .foundry/failure-modes.yaml under llm.max_calls_per_audit."
            )
        self._count += 1


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (extract→judge→continue)
# ---------------------------------------------------------------------------

_FM3_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "refuted"]},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "reasoning"],
}

_FM4_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "refuted", "new"]},
        "reasoning": {"type": "string"},
        "claim_line": {"type": "integer"},
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["verdict", "reasoning"],
}


def build_prompt_items_for_fm3_candidates(
    candidates: list[Finding],
    stack_name: str,
    stack_version: str,
) -> list[PromptItem]:
    """Build one PromptItem per FM3 candidate for skill-first mode.

    Each item id is ``judge:fm3:<index>`` (0-based). Uses ``model_tier="deep"``
    matching the existing ``model_logical="opus"`` calls.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    items: list[PromptItem] = []
    for idx, candidate in enumerate(candidates):
        user = _build_fm3_user(candidate, stack_name, stack_version)
        items.append(
            PromptItem(
                id=f"judge:fm3:{idx}",
                system=FM3_SYSTEM_PROMPT,
                user=user,
                response_format=ResponseFormat(type="json", schema=_FM3_RESPONSE_SCHEMA),
                model_tier="deep",
            )
        )
    return items


def build_prompt_items_for_fm4_functions(
    candidates: list[tuple[str, int, int, str, Finding | None]],
) -> list[PromptItem]:
    """Build one PromptItem per FM4 candidate function for skill-first mode.

    *candidates* is a list of ``(file, start_line, end_line, function_body, regex_finding)``
    tuples. Each item id is ``judge:fm4:<index>`` (0-based).
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    items: list[PromptItem] = []
    for idx, (file, start_line, end_line, function_body, regex_finding) in enumerate(candidates):
        user = _build_fm4_user(file, start_line, end_line, function_body, regex_finding)
        items.append(
            PromptItem(
                id=f"judge:fm4:{idx}",
                system=FM4_SYSTEM_PROMPT,
                user=user,
                response_format=ResponseFormat(type="json", schema=_FM4_RESPONSE_SCHEMA),
                model_tier="deep",
            )
        )
    return items


def apply_fm3_judgment(
    candidate: Finding,
    response: dict[str, Any],
) -> Finding | None:
    """Apply a skill-first FM3 judgment (from the judgments envelope).

    Applies the same parsing and anti-hallucination logic as ``judge_fm3_candidate``.
    Returns the rewritten Finding or None (if refuted or malformed).
    """
    parsed = _parse_fm3_response(json.dumps(response))
    if parsed is None or parsed["verdict"] == "refuted":
        return None
    reasoning = str(parsed.get("reasoning", "")).strip()
    new_msg = f"{candidate.message} | LLM-confirmed: {reasoning}"
    return replace(candidate, message=new_msg, rule_id=f"{candidate.rule_id}.llm-confirmed")


def apply_fm4_judgment(
    file: str,
    start_line: int,
    end_line: int,  # noqa: ARG001 — kept for call-site symmetry with judge_fm4_function
    function_body: str,
    regex_finding: Finding | None,
    response: dict[str, Any],
) -> Finding | None:
    """Apply a skill-first FM4 judgment (from the judgments envelope).

    Applies the same parsing and logic as ``judge_fm4_function``.
    Returns a Finding or None (if refuted, malformed, or unsupported with no regex_finding).
    """
    parsed = _parse_fm4_response(json.dumps(response))
    if parsed is None or parsed["verdict"] == "refuted":
        return None
    if parsed["verdict"] == "supported" and regex_finding is not None:
        return replace(
            regex_finding,
            message=f"{regex_finding.message} | LLM-confirmed: {parsed.get('reasoning', '')}",
            rule_id=f"{regex_finding.rule_id}.llm-confirmed",
        )
    if parsed["verdict"] == "new":
        claim_line_val = parsed["claim_line"]
        assert isinstance(claim_line_val, int)
        body_lines = function_body.splitlines()
        idx = claim_line_val - start_line
        snippet = body_lines[idx] if 0 <= idx < len(body_lines) else ""
        severity_val = parsed["severity"]
        assert severity_val in ("low", "medium", "high")
        return Finding(
            mode="fm4",
            severity=severity_val,
            file=file,
            line=claim_line_val,
            message=f"LLM-detected unsupported claim: {parsed.get('reasoning', '')}",
            snippet=snippet,
            rule_id="fm4.llm-judged.unsupported-claim",
        )
    return None


def serialize_finding(f: Finding) -> dict[str, Any]:
    """Serialize a Finding for intermediate storage (JSON-compatible)."""
    return asdict(f)


def deserialize_finding(d: dict[str, Any]) -> Finding:
    """Deserialize a Finding from intermediate storage."""
    return Finding(**d)

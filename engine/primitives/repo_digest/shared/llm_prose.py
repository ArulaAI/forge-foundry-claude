"""LLM prose augmentation for repo_digest (F1 — opt-in Opus narrative).

generate_architecture_prose() prepends a 2-3 sentence narrative to
architecture content; generate_deltas_prose() appends a trend summary
to recent-deltas content.  Both are fail-soft: malformed LLM responses
cause the base content to be returned unchanged.

The narrative is bounded non-deterministic analysis (a 2-3 sentence
summary Python cannot author), so it is legitimate model output — but it
is **grounded**: every back-ticked identifier the model emits must appear
in the deterministic ``base_content`` it summarizes, else it is replaced
with the ``⚠️ UNVERIFIED`` marker. This mirrors the Zero-Hallucination
guard in ``modernization_strategist.shared.narrative_synthesizer`` so the
prose cannot inject a fabricated module / path / symbol name into the
digest.

LlmProseCallCounter caps calls at config.llm.max_calls_per_run and raises
LlmProseBudgetExceededError when the budget is exhausted.
"""

from __future__ import annotations

import re
import sys
from typing import Any

PROSE_SYSTEM_PROMPT = (
    "You are generating a brief narrative summary to prepend or append to a "
    "machine-generated repo digest. Be concise (2-3 sentences). Only reference file, "
    "module, or symbol names that already appear in the supplied content. Respond in JSON: "
    '{"prose": "..."}.'
)

#: Matches a back-ticked token, e.g. ``\`payments_service.py\``.
_BACKTICK_RE = re.compile(r"`([^`]+)`")

#: Marker substituted for an un-grounded back-ticked name (matches the
#: narrative_synthesizer / playbook Zero-Hallucination convention).
_UNVERIFIED = "⚠️ UNVERIFIED"


def _guard_prose(prose: str, *, base_content: str, section: str) -> str:
    """Replace any back-ticked name absent from *base_content* with the UNVERIFIED marker.

    The deterministic ``base_content`` is the grounding universe: a
    legitimate narrative only names identifiers that already appear in the
    digest it summarizes. Anything else is a fabrication and is scrubbed.
    """

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in base_content:
            return match.group(0)
        warning = (
            f"prose for section={section!r} referenced un-grounded name "
            f"{token!r}; replacing with {_UNVERIFIED}"
        )
        print(f"[repo-digest] WARNING: {warning}", file=sys.stderr)
        return _UNVERIFIED

    return _BACKTICK_RE.sub(_sub, prose)


class LlmProseBudgetExceededError(RuntimeError):
    """Raised when prose LLM call count exceeds budget."""


class LlmProseCallCounter:
    def __init__(self, max_calls: int) -> None:
        self._max = max_calls
        self._count = 0

    @property
    def calls_made(self) -> int:
        return self._count

    def record_call(self) -> None:
        if self._count >= self._max:
            raise LlmProseBudgetExceededError(
                f"Prose LLM budget exceeded: {self._count} calls made, max={self._max}"
            )
        self._count += 1


def _build_user(section: str, base_content: str) -> str:
    return (
        f"section: {section}\n"
        f"content:\n{base_content}\n\n"
        "Generate a 2-3 sentence narrative summary."
    )


# ---------------------------------------------------------------------------
# Skill-first helpers (extract/continue protocol)
# ---------------------------------------------------------------------------


def build_prompt_items_for_prose(
    architecture_content: str | None,
    deltas_content: str | None,
) -> list[Any]:
    """Build prompt items for architecture + trend prose sections (skill-first mode).

    Only includes items where base_content is non-empty (no harness call
    is made for empty sections in the harness path either).

    Response format is JSON {"prose": "..."} matching PROSE_SYSTEM_PROMPT.

    Args:
        architecture_content: The deterministic architecture section text, or None.
        deltas_content: The deterministic recent-deltas section text, or None.

    Returns:
        List of 0-2 PromptItems. One per non-empty section.
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    prose_schema: dict[str, object] = {
        "type": "object",
        "properties": {"prose": {"type": "string"}},
        "required": ["prose"],
    }
    items: list[PromptItem] = []
    if architecture_content:
        items.append(
            PromptItem(
                id="narrative:architecture",
                system=PROSE_SYSTEM_PROMPT,
                user=_build_user("architecture", architecture_content),
                response_format=ResponseFormat(type="json", schema=prose_schema),
                model_tier="default",
            )
        )
    if deltas_content:
        items.append(
            PromptItem(
                id="narrative:recent-deltas",
                system=PROSE_SYSTEM_PROMPT,
                user=_build_user("recent-deltas", deltas_content),
                response_format=ResponseFormat(type="json", schema=prose_schema),
                model_tier="default",
            )
        )
    return items


def apply_prose_judgment(
    base_content: str,
    *,
    section: str,
    response: dict[str, object],
    position: str = "top",
) -> str:
    """Apply a pre-computed prose judgment with the grounding guard.

    Runs the same _guard_prose as generate_architecture_prose /
    generate_deltas_prose — back-ticked identifiers not in base_content
    are replaced with the UNVERIFIED marker.

    Args:
        base_content: The deterministic section text (grounding universe).
        section: Section name for warning messages.
        response: The model's JSON response dict {"prose": "..."}.
        position: "top" (prepend) or "bottom" (append).

    Returns:
        The augmented section text, or base_content unchanged if response
        is malformed.
    """
    prose_raw = response.get("prose") if isinstance(response, dict) else None
    if not isinstance(prose_raw, str):
        return base_content
    prose = _guard_prose(prose_raw, base_content=base_content, section=section)
    if position == "bottom":
        return f"{base_content}\n\n> {prose.strip()}\n"
    return f"> {prose.strip()}\n\n{base_content}"

"""Skill-first edit proposal builder for S2 test_maintainer.

Builds prompt items for the skill-first ``--extract-only`` / ``--continue`` protocol.
Each prompt item encodes one impacted (source, test) pair; the in-session model judges
whether to update / add / delete / skip the test, guided by optional S1 convention and
gap context (TM-4, #896).

Returns None when:
- The model response is malformed / invalid JSON.
- The model verdict is 'skip' (no edit needed).

Raises ProposerBudgetExceededError when the optional ProposerCallCounter is
exhausted.

S1/S2 composition seam (TM-4):
  build_prompt_item_for_test() accepts an optional ``s1_findings`` argument
  (a ``S1Findings`` parsed from ``s1-findings.json``).  When present, the user
  prompt gains a ``--- s1_conventions ---`` block (house style) and a
  ``--- s1_gaps ---`` block (blast-radius gaps for this symbol) so the model
  can guide 'update' edits toward house style and 'add' edits toward real gaps.
  When absent, the prompt is identical to the pre-TM-4 shape (non-breaking).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from primitives.test_maintainer.shared.edit_proposal import EditProposal, Verdict

if TYPE_CHECKING:
    from primitives.test_maintainer.shared.impact import ImpactReport
    from primitives.test_maintainer.shared.s1_compose import S1Findings

PROPOSER_SYSTEM_PROMPT = (
    "You are a test maintainer. Given a source-file diff (before/after) and "
    "an affected test, generate the minimal edit to keep the test correct.\n"
    'Respond in JSON: {"verdict": "update|add|delete|skip", '
    '"after_content": "...", "reasoning": "1-3 sentences"}.\n'
    "Rules:\n"
    "- 'update': modify existing test; provide the FULL new test file content as after_content.\n"
    "  When s1_conventions are provided, match the inferred framework, naming,\n"
    "  fixture/mock idioms, and assertion style so the edit fits house style.\n"
    "- 'add': propose a net-new test; provide its full content as after_content.\n"
    "  When s1_gaps are provided, prioritise closing a real in-style gap from that list.\n"
    "- 'delete': test target was removed; after_content should be empty string.\n"
    "- 'skip': no edit needed; after_content can be empty.\n"
    "Do not change test intent or assertions unrelated to the diff."
)

_VALID_VERDICTS = {"update", "add", "delete", "skip"}


class ProposerBudgetExceededError(RuntimeError):
    """Raised when LLM call count exceeds the budget."""


class ProposerCallCounter:
    def __init__(self, max_calls: int) -> None:
        self._max = max_calls
        self._count = 0

    @property
    def calls_made(self) -> int:
        return self._count

    def record_call(self) -> None:
        if self._count >= self._max:
            raise ProposerBudgetExceededError(
                f"Proposer LLM budget exceeded: {self._count} calls made, max={self._max}"
            )
        self._count += 1


def _parse_response(text: str) -> dict[str, str] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    verdict = data.get("verdict")
    if verdict not in _VALID_VERDICTS:
        return None
    after = data.get("after_content", "")
    reasoning = data.get("reasoning", "")
    if not isinstance(after, str) or not isinstance(reasoning, str):
        return None
    return {"verdict": verdict, "after_content": after, "reasoning": reasoning}


def _build_s1_conventions_block(s1_findings: S1Findings) -> str:
    """Render the s1_conventions context block for the user prompt.

    The block is inserted between the impact metadata and the source diff so the
    model sees house-style guidance before it reads the code.
    """
    c = s1_findings.conventions
    lines = [
        "--- s1_conventions ---",
        f"framework: {c.framework}",
        f"layout: {c.layout}",
        f"naming: {c.naming}",
        f"fixture_mock_idioms: {c.fixture_mock_idioms}",
        f"assertion_style: {c.assertion_style}",
    ]
    for k, v in sorted(c.extra.items()):
        lines.append(f"{k}: {v}")
    return "\n".join(lines) + "\n"


def _build_s1_gaps_block(s1_findings: S1Findings, affected_functions: tuple[str, ...]) -> str:
    """Render the s1_gaps context block for the user prompt.

    Only gaps whose symbol matches one of *affected_functions* are included —
    scoped to this (source, test) pair's blast radius.  When no matching gaps
    exist the block is omitted (returns empty string).
    """
    symbol_set = set(affected_functions)
    matching_gaps = [g for g in s1_findings.gaps if g.symbol in symbol_set]
    if not matching_gaps:
        return ""

    lines = ["--- s1_gaps ---"]
    for gap in matching_gaps:
        lines.append(f"symbol: {gap.symbol}")
        lines.append(f"gap_type: {gap.gap_type}")
        lines.append(f"description: {gap.description}")
        if gap.suggested_test_name:
            lines.append(f"suggested_test_name: {gap.suggested_test_name}")
        lines.append("")
    return "\n".join(lines)


def _build_user(
    impact: ImpactReport,
    source_before: str,
    source_after: str,
    test_content: str,
    s1_findings: S1Findings | None = None,
) -> str:
    impacts_str = ",".join(sorted(c.value for c in impact.impacts))
    affected = ",".join(impact.affected_functions)
    parts = [
        f"test_file: {impact.test_file}\nimpacts: {impacts_str}\naffected_functions: {affected}\n",
    ]

    # S1 context blocks (TM-4): inserted before the source diff so the model
    # reads house-style and gap guidance before encountering raw code.
    if s1_findings is not None:
        parts.append(_build_s1_conventions_block(s1_findings))
        gaps_block = _build_s1_gaps_block(s1_findings, impact.affected_functions)
        if gaps_block:
            parts.append(gaps_block + "\n")

    parts.append(
        f"--- source_before ---\n{source_before}\n"
        f"--- source_after ---\n{source_after}\n"
        f"--- test_content ---\n{test_content}\n"
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (--extract-only / --continue paths)
# ---------------------------------------------------------------------------

_PROPOSAL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["update", "add", "delete", "skip"]},
        "after_content": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "after_content", "reasoning"],
}


def build_prompt_item_for_test(
    impact: ImpactReport,
    source_before: str,
    source_after: str,
    test_content: str,
    s1_findings: S1Findings | None = None,
) -> tuple[str, str, str]:
    """Build a skill-first prompt item for an impacted test file.

    Returns (item_id, system_prompt, user_prompt) — the caller wraps these
    in PromptItem with response_format and model_tier="deep".

    Item id is ``"narrative:<impact.test_file>"`` (relative or absolute path
    as a string — callers normalise to a project-relative string before
    constructing the id, so the SKILL.md body can round-trip the path).

    S1/S2 composition (TM-4): when *s1_findings* is supplied (loaded from
    ``s1-findings.json`` written by S1 scoped-analysis), the user prompt gains
    a ``--- s1_conventions ---`` block (house style) and a ``--- s1_gaps ---``
    block (blast-radius gaps scoped to this symbol).  When *s1_findings* is
    None the prompt is identical to the pre-TM-4 shape — fully non-breaking.
    """
    item_id = f"narrative:{impact.test_file}"
    user = _build_user(impact, source_before, source_after, test_content, s1_findings)
    return item_id, PROPOSER_SYSTEM_PROMPT, user


def apply_proposal_judgment(
    impact: ImpactReport,
    response: list[Any] | dict[str, Any] | str,
    test_content: str,
) -> EditProposal | None:
    """Apply a pre-computed judgment response from the skill-first --continue path.

    The judgment is the in-session model's verdict (the Python CLI never calls
    an LLM): ``--extract-only`` emits the prompt, the agent session produces the
    response, and ``--continue --judgments`` feeds it here for deterministic
    validation.

    Returns an EditProposal or None (skip verdict / malformed response). The
    anti-hallucination guard runs the same ``_parse_response`` validation as the
    proposer: verdict membership + type constraints. Lists (unexpected array
    responses) are treated as malformed → None.
    """
    if isinstance(response, list):
        return None
    raw = json.dumps(response) if isinstance(response, dict) else str(response)
    parsed = _parse_response(raw)
    if parsed is None:
        return None
    verdict_val = parsed["verdict"]
    if verdict_val == "skip":
        return None
    return EditProposal(
        test_file=impact.test_file,
        verdict=Verdict(verdict_val),
        before_content=test_content,
        after_content=parsed["after_content"],
        reasoning=parsed["reasoning"],
        impact_classes=frozenset(c.value for c in impact.impacts),
    )

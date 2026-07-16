"""M3 Stage E — the per-context narrative prompt + judgment helpers.

The strategist's deterministic Stages A-D produce a fully-structured
:class:`DecompositionPlan`. This module is the *only* place the model is
consulted: it asks for a short prose narrative per :class:`BoundedContext`
explaining what the context is and why the rewrite should treat it as a unit —
the irreducible interpretive bridge Python can't synthesize.

``build_prompt_items_for_contexts`` emits one prompt **per context**, each fed a
*bounded compact* input (that context's name + its tables + its ownership slice
+ the antipatterns whose evidence touches it) rather than the whole plan, so the
prompt count equals the context count exactly and the per-call token cost stays
flat regardless of overall plan size (token-offload principle).

Anti-hallucination guard
-------------------------

The model is told the closed *universe* of names (every table and context in the
plan) and instructed to reference only those. ``apply_narrative_judgments``
scrubs each returned narrative: every back-tick-quoted ``name`` token that is
**not** in the plan's universe is replaced with ``⚠️ UNVERIFIED`` and a warning
is logged (module logger + stderr, matching the M1 ``lob_classifier``
discipline). The model cannot smuggle an invented table/context name into the
plan's prose.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from primitives.modernization_strategist.shared.models import (
        BoundedContext,
        DecompositionPlan,
    )

logger = logging.getLogger(__name__)

#: Matches a back-tick-quoted ``name`` token in the model's narrative.
_BACKTICK_RE = re.compile(r"`([^`]+)`")

_UNVERIFIED = "⚠️ UNVERIFIED"


SYSTEM_PROMPT = """\
You are a senior modernization architect writing a short narrative for ONE bounded \
context in a database-decomposition plan. You will be given that context's name, its \
tables, which tables it owns vs. shares, and any decomposition antipatterns that touch \
it — plus the CLOSED list of every table and context name in the plan.

Your task: write 2-4 sentences of prose describing what this context represents and the \
key consideration for extracting it as an independent service. Be concrete and grounded.

Rules:

1. Reference ONLY names from the supplied "universe" list. When you name a table or \
context, wrap it in back-ticks (e.g. `orders`). Do NOT invent tables, contexts, columns, \
or relationships the input does not list — a downstream guard will flag any back-ticked \
name outside the universe as UNVERIFIED.
2. Return PLAIN PROSE only — no Markdown headings, no bullet lists, no JSON.
3. If the input is thin, say so plainly rather than inventing detail.
"""


def _build_universe(plan: DecompositionPlan) -> set[str]:
    """Every name the model is allowed to reference in a grounded narrative.

    Includes context names, table names, AND every antipattern *evidence* token
    (#1197 6c): the antipattern evidence the model is handed cites stored-proc
    names (e.g. ``sp_assess_late_penalties``) and the contexts they span. Those SP
    names were previously excluded from the universe, so a narrative that
    (correctly) referenced them got flagged ``⚠️ UNVERIFIED`` — while the same
    names rendered un-flagged in the deterministic antipattern tables. Grounding
    the evidence tokens closes that inconsistency.
    """
    names: set[str] = {ctx.name for ctx in plan.contexts}
    for ctx in plan.contexts:
        names.update(ctx.tables)
    for ap in plan.antipatterns:
        names.update(str(token) for token in ap.evidence)
    return names


def _context_antipatterns(ctx: BoundedContext, plan: DecompositionPlan) -> list[dict[str, object]]:
    """Antipatterns whose evidence names a table in *ctx* (its relevant slice)."""
    ctx_tables = set(ctx.tables)
    slice_: list[dict[str, object]] = []
    for ap in plan.antipatterns:
        if ctx_tables & set(ap.evidence):
            slice_.append({"kind": ap.kind, "detail": ap.detail, "evidence": list(ap.evidence)})
    return slice_


def build_user_prompt(ctx: BoundedContext, plan: DecompositionPlan, universe: set[str]) -> str:
    """Render the bounded compact user message for one context.

    Deterministic + JSON-serialised so golden-file tests can key canned
    responses on the prompt and layout drift can't change the hash.
    """
    owned = set(plan.ownership.owned)
    shared = set(plan.ownership.shared)
    payload: dict[str, object] = {
        "context": ctx.name,
        "rationale": ctx.rationale,
        "tables": list(ctx.tables),
        "owned_tables": sorted(t for t in ctx.tables if t in owned),
        "shared_tables": sorted(t for t in ctx.tables if t in shared),
        "antipatterns": _context_antipatterns(ctx, plan),
        "universe": sorted(universe),
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _guard_narrative(raw: str, *, universe: set[str], context_name: str) -> str:
    """Replace any back-ticked name outside *universe* with the UNVERIFIED marker."""

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        if token in universe:
            return match.group(0)
        warning = (
            f"Stage E narrative for context={context_name!r} referenced unknown name "
            f"{token!r}; replacing with {_UNVERIFIED}"
        )
        logger.warning(warning)
        return _UNVERIFIED

    return _BACKTICK_RE.sub(_sub, raw)


# ---------------------------------------------------------------------------
# Skill-first protocol helpers (--extract-only / --continue paths)
# ---------------------------------------------------------------------------


def build_prompt_items_for_contexts(
    plan: DecompositionPlan,
) -> list[Any]:
    """Build one PromptItem per bounded context for skill-first mode.

    Each item uses response_format.type="text" — the model's prose IS the
    deliverable. The system and user prompts are identical to the harness path
    (SYSTEM_PROMPT, build_user_prompt) so the anti-hallucination guard applies
    identically on both paths.

    Args:
        plan: The Stage-A-D decomposition (all contexts resolved).

    Returns:
        List of PromptItem, one per context, id prefixed "narrative:".
    """
    from primitives._shared.protocol import PromptItem, ResponseFormat  # noqa: PLC0415

    universe = _build_universe(plan)
    items: list[PromptItem] = []
    for ctx in plan.contexts:
        user = build_user_prompt(ctx, plan, universe)
        items.append(
            PromptItem(
                id=f"narrative:{ctx.name}",
                system=SYSTEM_PROMPT,
                user=user,
                response_format=ResponseFormat(type="text"),
                model_tier="default",
            )
        )
    return items


def apply_narrative_judgments(
    plan: DecompositionPlan,
    judgments: dict[str, str],
) -> dict[str, str]:
    """Apply pre-computed narrative judgments. Anti-hallucination guard applied.

    Args:
        plan: The Stage-A-D decomposition (for universe + guard).
        judgments: dict mapping item id (e.g. "narrative:customer") to the
            plain-text narrative string from the judgments envelope.

    Returns:
        {context_name: guarded_narrative} — same shape as synthesize_narratives.
        Missing context ids produce an empty string (fail-open).
    """
    universe = _build_universe(plan)
    out: dict[str, str] = {}
    for ctx in plan.contexts:
        item_id = f"narrative:{ctx.name}"
        raw = judgments.get(item_id, "")
        out[ctx.name] = _guard_narrative(raw, universe=universe, context_name=ctx.name)
    return out


__all__ = [
    "SYSTEM_PROMPT",
    "apply_narrative_judgments",
    "build_prompt_items_for_contexts",
    "build_user_prompt",
]

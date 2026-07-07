"""M4 Stage D — ``render_plan_md`` (deterministic ``plan.md``).

Renders the human-readable migration plan from the Stage B
:class:`ConversionPlan`. Every line is produced by the Layer-1 render helpers
(:func:`heading` / :func:`md_table`) — Markdown text is Python-rendered, not
model-emitted (token-offload). No LLM, no clock, no filesystem; the same plan
always renders the same bytes (golden-file testable).

Sections: a header (SP + target + effort badge), the translation tables
(data deps, transaction boundaries, cursor/side-effect mappings), the suggested
commit sequence, the per-stage test plan, the migration gotchas, and the
per-axis effort factors (the calibration audit trail).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._shared.modernization import heading, md_table

if TYPE_CHECKING:
    from primitives.sp_converter.shared.models import ConversionPlan


def _bullets(items: list[str], *, empty: str) -> str:
    """Render *items* as a Markdown bullet list, or *empty* when there are none."""
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def _effort_factors_table(plan: ConversionPlan) -> str:
    """Render the per-axis effort factors verbatim (calibration audit trail)."""
    rows = [[axis, f"{value:g}"] for axis, value in sorted(plan.effort.factors.items())]
    return md_table(["Axis", "Score"], rows)


def render_plan_md(plan: ConversionPlan) -> str:
    """Render the ``plan.md`` body as Markdown text.

    Args:
        plan: the Stage B :class:`ConversionPlan` (with the Stage E effort
            estimate folded in).

    Returns:
        The Markdown document as a single string (trailing newline). Pure +
        deterministic.
    """
    parts: list[str] = [
        heading(f"Conversion plan — {plan.sp_name}", 1),
        f"**Target:** `{plan.target}`  •  "
        f"**Effort:** {plan.effort.size} (score {plan.effort.score})",
        f"_{plan.effort.rationale}_",
        heading("Data dependencies", 2),
        _bullets(plan.data_deps, empty="No resolved data dependencies."),
        heading("Transaction boundaries", 2),
        _bullets(
            plan.txn_boundaries,
            empty="No transaction-boundary translation notes.",
        ),
        heading("Cursor & side-effect translation", 2),
    ]

    if plan.side_effect_mappings:
        rows = [[kind, api] for kind, api in sorted(plan.side_effect_mappings.items())]
        parts.append(md_table(["Side effect", "Target API"], rows))
    else:
        parts.append("No side effects to translate.")

    parts += [
        heading("Suggested commit sequence", 2),
        _bullets(plan.commit_sequence, empty="No commit sequence suggested."),
        heading("Test plan", 2),
        _bullets(plan.test_plan, empty="No test cases suggested."),
        heading("Gotchas", 2),
        _bullets(plan.gotchas, empty="No migration gotchas recorded."),
        heading("Per-axis effort factors", 2),
        _effort_factors_table(plan),
    ]

    return "\n\n".join(parts) + "\n"


__all__ = ["render_plan_md"]

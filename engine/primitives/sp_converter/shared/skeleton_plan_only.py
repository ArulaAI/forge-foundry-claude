"""M4 Stage C — the ``plan-only`` skeleton renderer.

The always-available default: emits no scaffold at all (the engineer wanted the
``plan.md`` + effort estimate without committing to a framework). Returns the
empty artifact list. Deterministic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.sp_converter.shared.skeletons import register

if TYPE_CHECKING:
    from primitives.sp_converter.shared.models import ConversionArtifact, ConversionPlan


def render(plan: ConversionPlan) -> list[ConversionArtifact]:  # noqa: ARG001 — registry contract
    """Render the ``plan-only`` target — no scaffold, always ``[]``."""
    return []


register("plan-only", render)

__all__ = ["render"]

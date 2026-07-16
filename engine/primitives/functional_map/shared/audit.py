from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.functional_map.shared.models import FunctionalDigest
    from primitives.functional_map.shared.synthesis_prompts import Synthesis

FLOOR = 0.6


@dataclass(frozen=True)
class AuditResult:
    accuracy: float
    unresolved: tuple[str, ...]
    #: FR ids that have no acceptance criterion (coverage gap — warning, not exit-6).
    uncovered_frs: tuple[str, ...] = ()

    @property
    def below_floor(self) -> bool:
        return self.accuracy < FLOOR


def _uncovered_frs(syn: Synthesis) -> tuple[str, ...]:
    """FR ids that the synthesis authored but never gave an acceptance criterion.

    Every FR is expected to carry ≥1 AC (SKILL.md coverage mandate); the ones that
    don't are surfaced so the gap is visible instead of silently under-covered.
    """
    covered = {ac.fr_id for ac in syn.acceptance_criteria}
    return tuple(fr.id for fr in syn.fr_statements if fr.id not in covered)


def audit_synthesis(digest: FunctionalDigest, syn: Synthesis) -> AuditResult:
    by_id = {e.id: (e.file, e.line) for e in digest.endpoints}
    total = 0
    unresolved: list[str] = []
    for flow in syn.flows:
        for step in flow.steps:
            total += 1
            want = by_id.get(step.endpoint_id)
            if want != (step.file, step.line):
                unresolved.append(
                    f"{flow.id} step {step.order}: "
                    f"{step.endpoint_id}@{step.file}:{step.line} unresolved"
                )
    accuracy = 1.0 if total == 0 else (total - len(unresolved)) / total
    return AuditResult(
        accuracy=accuracy,
        unresolved=tuple(unresolved),
        uncovered_frs=_uncovered_frs(syn),
    )

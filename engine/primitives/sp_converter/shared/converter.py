"""M4 orchestrator — assemble the conversion result and persist it.

The Stage B conversion plan is produced in-session via the skill-first
extract→judge→continue protocol (see
:mod:`primitives.sp_converter.shared.plan_synthesizer`); the CLI assembles the
:class:`ConversionResult` from the judged plan + the deterministic skeleton
render. :func:`write_conversion` persists ``conversion-plan.json``, ``plan.md``,
and the per-target skeleton artifacts under the output directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.sp_converter.shared.models import (
        ConversionArtifact,
        ConversionPlan,
    )

_PLAN_JSON = "conversion-plan.json"
_PLAN_MD = "plan.md"


@dataclass(frozen=True)
class ConversionResult:
    """The full M4 output for one SP: the plan, the skeleton, the rendered notes."""

    plan: ConversionPlan
    artifacts: list[ConversionArtifact]
    plan_md: str


def write_conversion(result: ConversionResult, out_dir: Path) -> list[Path]:
    """Write ``conversion-plan.json`` + ``plan.md`` + skeleton artifacts to *out_dir*.

    Returns the list of written paths, in write order, so the CLI can echo a
    ``WROTE <path>`` line per artifact.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    plan_json = out_dir / _PLAN_JSON
    plan_json.write_text(result.plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    written.append(plan_json)

    plan_md = out_dir / _PLAN_MD
    plan_md.write_text(result.plan_md, encoding="utf-8")
    written.append(plan_md)

    for artifact in result.artifacts:
        dest = out_dir / artifact.path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(artifact.content, encoding="utf-8")
        written.append(dest)

    return written


__all__ = [
    "ConversionResult",
    "write_conversion",
]

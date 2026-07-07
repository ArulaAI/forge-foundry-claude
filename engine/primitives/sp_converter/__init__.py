"""M4 /mod-sp-converter — public API.

Re-exports the Pydantic model surface + the orchestrator entry point. M4 is a
Python-load-bearing primitive (v3 PLB flip, #645 + #646): the five-stage
conversion pipeline lives in :mod:`primitives.sp_converter.shared`, surfaced as
the ``forge-validate mod-sp-converter`` command; the ``skill/SKILL.md`` body
delegates to that CLI.

See ``primitives/sp_converter/SPEC.md`` for the primitive contract.
"""

from primitives.sp_converter.shared.converter import (
    ConversionResult,
    write_conversion,
)
from primitives.sp_converter.shared.models import (
    ConversionArtifact,
    ConversionPlan,
    EffortEstimate,
    Target,
)

__all__ = [
    "ConversionArtifact",
    "ConversionPlan",
    "ConversionResult",
    "EffortEstimate",
    "Target",
    "write_conversion",
]

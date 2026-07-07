"""C5 /spec-validate — Pydantic models.

All models use extra="forbid" (strict). AIQualityReport and datetime are kept
as runtime imports (noqa: TC001/TC003) so Pydantic can resolve field annotations.
No `Any` typing on Pydantic fields.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic field, runtime-required
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# Runtime import is safe: ai_quality_aggregator's only import from this module
# (AcceptanceTrace) is itself TYPE_CHECKING-guarded, so there is no cycle.
from primitives.spec_validate.shared.ai_quality_aggregator import (  # noqa: TC001
    AIQualityReport,  # Pydantic field in SpecValidateReport, runtime-required
)

# ---------------------------------------------------------------------------
# Literal type aliases
# ---------------------------------------------------------------------------

MatchStatus: TypeAlias = Literal["satisfied", "partial", "unsatisfied", "not_applicable"]

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

Confidence: TypeAlias = Literal["low", "medium", "high"]

CodeElementKind: TypeAlias = Literal[
    "function",
    "class",
    "method",
    "endpoint",
    "test",
    "constant",
    "other",
]

# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class CodeElementRef(BaseModel):
    """A reference to a code element in the implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    line: int
    name: str
    kind: CodeElementKind


class AcceptanceTrace(BaseModel):
    """A single acceptance criterion mapped to its implementation status."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    criterion_id: str
    criterion_text: str
    status: MatchStatus
    code_elements: list[CodeElementRef] = Field(default_factory=list)
    rationale: str
    severity: Severity
    confidence: Confidence


class ScopeDriftItem(BaseModel):
    """A public-API feature in the implementation not mentioned in the spec."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    line: int
    name: str
    kind: CodeElementKind
    description: str


class SpecValidateReport(BaseModel):
    """Full output of the C5 spec-validate pipeline."""

    model_config = ConfigDict(extra="forbid")

    spec_path: str
    code_path: str
    traces: list[AcceptanceTrace] = Field(default_factory=list)
    scope_drift: list[ScopeDriftItem] = Field(default_factory=list)
    # Forward reference; AIQualityReport is concrete at runtime but defined in
    # a sibling module. See TYPE_CHECKING block at top + model_rebuild() below.
    ai_quality: AIQualityReport | None = None
    generated_at: datetime

    @property
    def has_unsatisfied(self) -> bool:
        """True if any trace is unsatisfied or partial."""
        return any(t.status in ("unsatisfied", "partial") for t in self.traces)

    @property
    def has_critical_ai_quality(self) -> bool:
        """True if sub-call C10/C11 returned any critical finding (after dedup)."""
        if self.ai_quality is None:
            return False
        return self.ai_quality.has_critical

    @property
    def status_counts(self) -> dict[str, int]:
        """Count of traces grouped by match status."""
        counts: dict[str, int] = {
            "satisfied": 0,
            "partial": 0,
            "unsatisfied": 0,
            "not_applicable": 0,
        }
        for trace in self.traces:
            counts[trace.status] += 1
        return counts

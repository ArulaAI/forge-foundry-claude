"""C4 /code-review — Pydantic models.

All models use extra="forbid" (strict). AIQualityReport and datetime are kept
as runtime imports (noqa: TC001/TC003) so Pydantic can resolve field annotations.
No `Any` typing on Pydantic fields.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic field, runtime-required
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# Runtime import is safe: ai_quality_aggregator's only import from this module
# is itself TYPE_CHECKING-guarded, so there is no cycle.
from primitives.code_review.shared.ai_quality_aggregator import (  # noqa: TC001
    AIQualityReport,  # Pydantic field in CodeReviewReport, runtime-required
)

# ---------------------------------------------------------------------------
# Literal type aliases
# ---------------------------------------------------------------------------

Lens: TypeAlias = Literal[
    "bug",
    "error_handling",
    "convention",
    "documentation",
    "security_smell",
    "test_quality",
]

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

Confidence: TypeAlias = Literal["low", "medium", "high"]

# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class CodeReviewIssue(BaseModel):
    """A single line-level code review finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lens: Lens
    file: str
    line: int
    end_line: int | None = None
    severity: Severity
    confidence: Confidence
    confidence_score: float = Field(ge=0.0, le=1.0)
    title: str
    description: str
    suggestion: str | None = None
    cwe_ids: list[str] = Field(default_factory=list)
    # #425: control IDs cross-referenced from S70's compliance.yaml when
    # the finding's file is also flagged by S60's data-flow.json. Empty
    # list when either upstream artifact is absent (graceful degradation).
    linked_controls: list[str] = Field(default_factory=list)


class CodeReviewReport(BaseModel):
    """Full output of the C4 code-review pipeline."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    code_path: str
    lenses: list[Lens]
    issues: list[CodeReviewIssue] = Field(default_factory=list)
    # Forward reference; AIQualityReport is concrete at runtime but defined in
    # a sibling module. See TYPE_CHECKING block at top + model_rebuild() below.
    ai_quality: AIQualityReport | None = None
    generated_at: datetime

    @property
    def has_critical_issues(self) -> bool:
        """True if any C4 issue is critical severity."""
        return any(i.severity == "critical" for i in self.issues)

    @property
    def has_critical_ai_quality(self) -> bool:
        """True if sub-call C10/C11 returned any critical finding (after dedup)."""
        if self.ai_quality is None:
            return False
        return self.ai_quality.has_critical

    @property
    def issue_count_by_severity(self) -> dict[str, int]:
        """Count of issues grouped by severity."""
        counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for issue in self.issues:
            counts[issue.severity] += 1
        return counts

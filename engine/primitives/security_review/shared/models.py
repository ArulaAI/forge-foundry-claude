"""S11 /security-review — Pydantic models.

All models use extra="forbid" (strict). AIQualityReport and datetime are kept
as runtime imports (noqa: TC001/TC003) so Pydantic can resolve field annotations.
No `Any` typing on Pydantic fields — C2 regression guard.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic field, runtime-required
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# Runtime import is safe: ai_quality_aggregator's only import from this module
# is itself TYPE_CHECKING-guarded, so there is no cycle.
from primitives.security_review.shared.ai_quality_aggregator import (  # noqa: TC001
    AIQualityReport,  # Pydantic field in SecurityReviewReport, runtime-required
)

# ---------------------------------------------------------------------------
# Literal type aliases — C1 regression guard: each must appear in its prompt
# ---------------------------------------------------------------------------

SecurityLens: TypeAlias = Literal[
    "authn",
    "authz",
    "crypto",
    "input_handling",
    "data_exposure",
    "secrets_mgmt",
    "owasp_top10",
]

OWASPCategory: TypeAlias = Literal[
    "A01", "A02", "A03", "A04", "A05", "A06", "A07", "A08", "A09", "A10"
]

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

Confidence: TypeAlias = Literal["low", "medium", "high"]

# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class SecurityFinding(BaseModel):
    """A single security finding from a lens review."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lens: SecurityLens
    file: str
    line: int
    end_line: int | None = None
    severity: Severity
    confidence: Confidence
    confidence_score: float = Field(ge=0.0, le=1.0)
    title: str
    description: str
    suggestion: str | None = None
    owasp_category: OWASPCategory | None = None
    cwe_ids: list[str] = Field(default_factory=list)
    compliance_refs: list[str] = Field(default_factory=list)


class SensitiveArea(BaseModel):
    """A file or directory identified as security-sensitive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    kind: Literal["auth", "crypto", "secret", "data", "general"]
    reason: str


class SecurityReviewReport(BaseModel):
    """Full output of the S11 security-review pipeline."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    code_path: str
    lenses: list[SecurityLens]
    sensitive_areas: list[SensitiveArea] = Field(default_factory=list)
    findings: list[SecurityFinding] = Field(default_factory=list)
    # Forward reference; AIQualityReport is concrete at runtime but defined in
    # a sibling module. See TYPE_CHECKING block at top + model_rebuild() below.
    ai_quality: AIQualityReport | None = None
    generated_at: datetime

    @property
    def has_critical_findings(self) -> bool:
        """True if any S11 finding is critical severity."""
        return any(f.severity == "critical" for f in self.findings)

    @property
    def has_critical_ai_quality(self) -> bool:
        """True if sub-call C10/C11 returned any critical finding (after dedup)."""
        if self.ai_quality is None:
            return False
        return self.ai_quality.has_critical

    @property
    def finding_count_by_severity(self) -> dict[str, int]:
        """Count of findings grouped by severity."""
        counts: dict[str, int] = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

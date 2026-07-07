"""S40 /api-contract-review — Pydantic models.

All models use extra="forbid" (strict). AIQualityReport and datetime are kept
as runtime imports (noqa: TC001/TC003) so Pydantic can resolve field annotations.
No `Any` typing on Pydantic fields — C2 regression guard.
"""

from __future__ import annotations

import re
from datetime import datetime  # noqa: TC003 — Pydantic field, runtime-required
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Runtime import is safe: ai_quality_aggregator's only import from this module
# is itself TYPE_CHECKING-guarded, so there is no cycle.
from primitives.api_contract_review.shared.ai_quality_aggregator import (  # noqa: TC001
    AIQualityReport,  # Pydantic field in APIContractReviewReport, runtime-required
)

# ---------------------------------------------------------------------------
# Literal type aliases — C1 regression guard: each must appear in its prompt
# ---------------------------------------------------------------------------

ContractFormat: TypeAlias = Literal[
    "openapi-3.0",
    "openapi-3.1",
    "asyncapi-2",
    "asyncapi-3",
    "graphql",
]

APILens: TypeAlias = Literal[
    "versioning",
    "error_shapes",
    "auth_spec",
    "idempotency",
    "backward_compatibility",
    "pagination",
    "rate_limiting",
]

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

Confidence: TypeAlias = Literal["low", "medium", "high"]

# ---------------------------------------------------------------------------
# rfc_ref regex — anti-hallucination 3: RFC NNNNN or OWASP <text>
# ---------------------------------------------------------------------------

_RFC_REF_RE = re.compile(r"^RFC\s?\d{3,5}$|^OWASP\s.*$")

RFC_REF_PATTERN = _RFC_REF_RE.pattern

# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class Endpoint(BaseModel):
    """An API endpoint extracted from a contract file.

    REST: path + method (e.g., GET /users)
    AsyncAPI: channel + operation (e.g., publish user.created)
    GraphQL: type + field (e.g., Query.users)
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    method_or_op: str
    format: ContractFormat
    file: str
    line: int


class APIFinding(BaseModel):
    """A single API contract review finding for a given lens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    lens: APILens
    file: str
    line: int
    endpoint: str | None = None
    severity: Severity
    confidence: Confidence
    confidence_score: float = Field(ge=0.0, le=1.0)
    title: str
    description: str
    suggestion: str | None = None
    rfc_ref: str | None = None
    breaking: bool = False

    @field_validator("rfc_ref")
    @classmethod
    def validate_rfc_ref(cls, v: str | None) -> str | None:
        """Validate rfc_ref format; must match RFC NNNNN or OWASP <text>."""
        if v is not None and not _RFC_REF_RE.match(v):
            raise ValueError(
                f"rfc_ref must match 'RFC NNNNN' or 'OWASP ...' (e.g., RFC 7807, OWASP A01), "
                f"got {v!r}"
            )
        return v


class BaselineDiff(BaseModel):
    """Backward-compatibility diff result against a baseline git ref."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    removed: list[str] = Field(default_factory=list)
    type_narrowed: list[str] = Field(default_factory=list)
    new_required_fields: list[str] = Field(default_factory=list)

    @property
    def has_breaking_changes(self) -> bool:
        """True if any breaking change detected."""
        return bool(self.removed or self.type_narrowed or self.new_required_fields)


class APIContractReviewReport(BaseModel):
    """Full output of the S40 api-contract-review pipeline."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    contract_path: str
    format: ContractFormat
    lenses: list[APILens]
    endpoints: list[Endpoint] = Field(default_factory=list)
    findings: list[APIFinding] = Field(default_factory=list)
    baseline_diff: BaselineDiff | None = None
    # Forward reference; AIQualityReport is concrete at runtime but defined in
    # a sibling module. See TYPE_CHECKING block at top + model_rebuild() below.
    ai_quality: AIQualityReport | None = None
    generated_at: datetime

    @property
    def has_critical_findings(self) -> bool:
        """True if any S40 finding is critical severity."""
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

    @property
    def finding_count_by_lens(self) -> dict[str, int]:
        """Count of findings grouped by lens."""
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.lens] = counts.get(finding.lens, 0) + 1
        return counts

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


# ---------------------------------------------------------------------------
# SP-F (#1386) — additive REST-API schema extraction models.
#
# These sit ALONGSIDE the review models above; the review path (lens_reviewer,
# report_renderer, findings, exit codes, §3.4 ai_quality sub-call) is untouched.
# The extraction reads only what the contract file DECLARES — no source-code
# inference — so it stays deterministic and honest. The `api.py` Layer-1 adapter
# threads a plain-dict serialisation of an ``ApiExtraction`` (never this model
# object) into the modernization slice, so QUAL-2's cross-primitive-shared guard
# stays green (Layer-1 must not import this Layer-2 model).
# ---------------------------------------------------------------------------


class FieldConstraint(BaseModel):
    """One field-level validation constraint the contract declares.

    Every non-name attribute defaults empty/None so a field that declares only
    ``required`` (and no bounds) still round-trips through ``extra="forbid"``.
    Only the constraints the contract *states* are populated — no inference.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    field: str
    type: str = ""
    required: bool = False
    format: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    enum: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        """A compact one-line human-readable constraint summary (e.g. for a table cell)."""
        parts: list[str] = []
        if self.type:
            parts.append(self.type)
        if self.required:
            parts.append("required")
        if self.format:
            parts.append(f"format={self.format}")
        if self.minimum is not None:
            parts.append(f"min={self.minimum:g}")
        if self.maximum is not None:
            parts.append(f"max={self.maximum:g}")
        if self.min_length is not None:
            parts.append(f"minLength={self.min_length}")
        if self.max_length is not None:
            parts.append(f"maxLength={self.max_length}")
        if self.pattern:
            parts.append(f"pattern={self.pattern}")
        if self.enum:
            parts.append("enum=" + "|".join(self.enum))
        detail = ", ".join(parts)
        return f"{self.field}: {detail}" if detail else self.field


class ResponseShape(BaseModel):
    """One declared response for an operation: a status code + its DTO + fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status_code: str
    description: str = ""
    schema_name: str = ""
    fields: list[FieldConstraint] = Field(default_factory=list)


class ExtractedOperation(BaseModel):
    """One operation (verb + path) extracted from the contract's declarations.

    Carries the request DTO shape, per-status response shapes, and the security
    requirement names that apply (per-operation ``security`` if declared, else the
    document-level default). All collections default empty.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    method: str
    path: str
    operation_id: str = ""
    summary: str = ""
    request_schema_name: str = ""
    request_fields: list[FieldConstraint] = Field(default_factory=list)
    responses: list[ResponseShape] = Field(default_factory=list)
    security: list[str] = Field(default_factory=list)

    @property
    def status_codes(self) -> list[str]:
        """The declared response status codes, in declaration order."""
        return [r.status_code for r in self.responses]


class SecurityScheme(BaseModel):
    """One entry from the contract's ``components.securitySchemes``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str = ""
    scheme: str = ""
    bearer_format: str = ""
    location: str = ""  # OpenAPI `in` (header/query/cookie) for apiKey schemes


class ApiExtraction(BaseModel):
    """Deterministic REST-API schema extraction — additive to the S40 review.

    Produced by
    :func:`~primitives.api_contract_review.shared.contract_parsers.openapi_parser.extract_operations`
    (and re-exported from the primitive's public surface). The Layer-1
    modernization ``api.py`` adapter consumes ``model_dump(mode="json")`` of this
    object (a plain dict) — it never imports this class — so the cross-primitive
    ``shared`` guard (QUAL-2) stays green.
    """

    model_config = ConfigDict(extra="forbid")

    contract_path: str = ""
    format: ContractFormat | None = None
    operations: list[ExtractedOperation] = Field(default_factory=list)
    security_schemes: list[SecurityScheme] = Field(default_factory=list)

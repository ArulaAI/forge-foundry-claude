"""S60 /data-flow-map — Pydantic models.

All models use extra="forbid" (strict). AIQualityReport and datetime are kept
as runtime imports (noqa: TC001/TC003) so Pydantic can resolve field annotations.
No `Any` typing.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic field, runtime-required
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict

# Runtime import is safe: ai_quality_aggregator's only import from this module
# is itself TYPE_CHECKING-guarded, so there is no cycle.
from primitives.data_flow_map.shared.ai_quality_aggregator import (  # noqa: TC001
    AIQualityReport,  # Pydantic field in DataFlowReport, runtime-required
)

# ---------------------------------------------------------------------------
# Literal type aliases
# ---------------------------------------------------------------------------

DataClassification: TypeAlias = Literal["pii", "pci", "phi", "internal", "public"]

Category: TypeAlias = Literal[
    "identity",
    "contact",
    "government_id",
    "payment_account",
    "payment_auth",
    "medical",
    "financial_account",
    "address",
    "credential",
    "biometric",
    "behavioral",
    "location",
]

SinkKind: TypeAlias = Literal[
    "logger",
    "http_client",
    "db_write",
    "response_body",
    "file_write",
    "queue",
    "cache",
]

RetentionStatus: TypeAlias = Literal["explicit", "missing", "expired"]

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

Confidence: TypeAlias = Literal["regex", "llm", "yaml"]


# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class PIISource(BaseModel):
    """A detected source of PII / sensitive data in the codebase."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    file: str
    line: int
    classification: DataClassification
    category: Category
    severity: Severity
    confidence: Confidence


class FlowEdge(BaseModel):
    """A directed data-flow edge in the flow graph."""

    model_config = ConfigDict(extra="forbid")

    from_node: str
    to_node: str
    transformation: str | None = None


class Sink(BaseModel):
    """A detected data sink in the codebase."""

    model_config = ConfigDict(extra="forbid")

    name: str
    file: str
    line: int
    kind: SinkKind
    references_pii: list[str]  # list of PIISource names
    source: str = "s60_static"  # provenance tag; c11_fm8 for folded findings


class RetentionMarker(BaseModel):
    """A retention annotation (explicit, missing, or expired) for a field/sink."""

    model_config = ConfigDict(extra="forbid")

    field_or_sink: str
    retention_period: str | None = None
    status: RetentionStatus
    reference: str  # GDPR article reference or custom label
    source: str = "s60_static"  # provenance tag; c11_fm8 for folded findings


class PurposeIssue(BaseModel):
    """A purpose-limitation gap detected by LLM (Stage E)."""

    model_config = ConfigDict(extra="forbid")

    field: str
    declared_purpose: str | None = None
    observed_use: str
    severity: Severity


class DataFlowReport(BaseModel):
    """Full output of the S60 data-flow-map pipeline."""

    model_config = ConfigDict(extra="forbid")

    code_path: str
    sources: list[PIISource]
    flows: list[FlowEdge]
    sinks: list[Sink]
    retention: list[RetentionMarker]
    purpose_issues: list[PurposeIssue]
    # Forward reference; AIQualityReport is concrete at runtime but defined in
    # a sibling module.  See TYPE_CHECKING block at top + model_rebuild() below.
    ai_quality: AIQualityReport | None = None
    generated_at: datetime

    @property
    def has_critical_ai_quality(self) -> bool:
        """True if sub-call C10/C11 returned any critical finding."""
        return self.ai_quality is not None and self.ai_quality.has_critical

    @property
    def has_retention_issues(self) -> bool:
        """True if any PIISource is missing an explicit retention marker."""
        return any(r.status != "explicit" for r in self.retention)

    @property
    def has_purpose_issues(self) -> bool:
        """True if any purpose-limitation issue was identified."""
        return len(self.purpose_issues) > 0

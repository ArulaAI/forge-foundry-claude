"""S70 /compliance-map — Pydantic data models.

All Literal types are tested for exhaustiveness. Every Literal value MUST appear
verbatim in the system prompts (prompts.py) that ask the LLM to return it (C1 guard).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# Runtime import is safe: ai_quality_aggregator's only import from this module
# is itself TYPE_CHECKING-guarded, so there is no cycle.
from primitives.compliance_map.shared.ai_quality_aggregator import (  # noqa: TC001
    AIQualityReport,  # Pydantic field in ComplianceReport, runtime-required
)

# ---------------------------------------------------------------------------
# Framework
# ---------------------------------------------------------------------------

Framework: TypeAlias = Literal[
    "pci-dss-4.0", "gdpr", "sox-404", "nist-800-53-rev5", "epa-environmental"
]

_ALL_FRAMEWORKS: tuple[Framework, ...] = (
    "pci-dss-4.0",
    "gdpr",
    "sox-404",
    "nist-800-53-rev5",
    "epa-environmental",
)

# ---------------------------------------------------------------------------
# ControlStatus
# ---------------------------------------------------------------------------

ControlStatus: TypeAlias = Literal["addressed", "partial", "unaddressed", "not_applicable"]

_ALL_CONTROL_STATUSES: tuple[ControlStatus, ...] = (
    "addressed",
    "partial",
    "unaddressed",
    "not_applicable",
)

# ---------------------------------------------------------------------------
# EvidenceConfidence
# ---------------------------------------------------------------------------

EvidenceConfidence: TypeAlias = Literal["regex", "llm", "spec"]

_ALL_EVIDENCE_CONFIDENCES: tuple[EvidenceConfidence, ...] = (
    "regex",
    "llm",
    "spec",
)

# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

_ALL_SEVERITIES: tuple[Severity, ...] = ("low", "medium", "high", "critical")

# ---------------------------------------------------------------------------
# Control
# ---------------------------------------------------------------------------


class Control(BaseModel):
    """A single compliance control from a framework catalog."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    framework: Framework
    title: str
    family: str
    severity: Severity
    evidence_hints: list[str] = Field(default_factory=list)
    code_signatures: list[str] = Field(default_factory=list)
    cwe_ids: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# EvidenceRef
# ---------------------------------------------------------------------------


class EvidenceRef(BaseModel):
    """A single piece of evidence for a control — file:line + snippet."""

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int
    snippet: str
    confidence: EvidenceConfidence


# ---------------------------------------------------------------------------
# ControlMatch
# ---------------------------------------------------------------------------


class ControlMatch(BaseModel):
    """A control matched against a spec + optional code path."""

    model_config = ConfigDict(extra="forbid")

    control: Control
    status: ControlStatus
    evidence: list[EvidenceRef] = Field(default_factory=list)
    gap_description: str | None = None
    remediation_owner: str = "engineering"


# ---------------------------------------------------------------------------
# Gap
# ---------------------------------------------------------------------------


class Gap(BaseModel):
    """A gap identified during Stage E analysis."""

    model_config = ConfigDict(extra="forbid")

    control: Control
    missing_evidence: str
    suggested_remediation: str
    severity: Severity


# ---------------------------------------------------------------------------
# AIQualityReport (imported from ai_quality_aggregator — declared here for type checking)
# ---------------------------------------------------------------------------

# NOTE: AIQualityReport lives in ai_quality_aggregator.py.
# ai_quality_aggregator does NOT import from this module (no cycle).

# ---------------------------------------------------------------------------
# ComplianceReport
# ---------------------------------------------------------------------------


class ComplianceReport(BaseModel):
    """Full compliance map report."""

    model_config = ConfigDict(extra="forbid")

    # Document schema version pinned for SOX/PCI audit replay (GOV-3, #619): a
    # consumer can refuse to deserialise a future v2 shape, and a SARIF/JSON file
    # produced by v3 S70 is distinguishable from one a later schema produces.
    schema_version: Literal["1.0"] = "1.0"
    spec_title: str
    frameworks: list[Framework]
    matches: list[ControlMatch] = Field(default_factory=list)
    gaps: list[Gap] = Field(default_factory=list)
    # Forward reference; AIQualityReport is concrete at runtime but defined in a sibling module.
    ai_quality: AIQualityReport | None = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

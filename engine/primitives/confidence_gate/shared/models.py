"""C6 /confidence-gate — Pydantic models.

All models use extra="forbid" (strict). No `Any` on Pydantic fields — C2 regression guard.
Literal types are exhaustive — C1 regression guard.
No bare except-Exception, no tuple-form multi-exception catches — C3/C4/S40 guard.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 -- Pydantic field, runtime-required
from typing import Literal, TypeAlias, get_args

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Literal type aliases — ALL values must appear verbatim in prompts (C1 guard)
# ---------------------------------------------------------------------------

ReportType: TypeAlias = Literal[
    "c4_code_review",
    "c5_spec_validate",
    "c8_architecture_review",
    "s10_threat_model",
    "s11_security_review",
    "s20_cloud_arch_review",
    "s30_frontend_design",
    "s40_api_contract_review",
    "s60_data_flow_map",
    "s70_compliance_map",
    "c10_failure_mode_audit",
    "c11_production_failure_audit",
]

Recommendation: TypeAlias = Literal["pass", "review", "fail"]

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

ConfidenceLevel: TypeAlias = Literal["low", "medium", "high"]

# Tuple forms of the Literal value sets — used for validation, not as a type
# annotation. Derived from the Literal definitions via ``typing.get_args`` so
# the two cannot drift apart (#103 / QUAL-5).
_ALL_REPORT_TYPES: tuple[str, ...] = get_args(ReportType)
_ALL_SEVERITIES: tuple[str, ...] = get_args(Severity)
_ALL_CONFIDENCE_LEVELS: tuple[str, ...] = get_args(ConfidenceLevel)
_ALL_RECOMMENDATIONS: tuple[str, ...] = get_args(Recommendation)

# ---------------------------------------------------------------------------
# Core models
# ---------------------------------------------------------------------------


class NormalizedFinding(BaseModel):
    """A finding normalized from any supported report type.

    Carries only the fields needed for risk scoring.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_type: ReportType
    severity: Severity
    confidence_level: ConfidenceLevel
    file: str | None = None
    line: int | None = None
    title: str = ""
    description: str = ""


class TopFinding(BaseModel):
    """A top risk finding selected by the LLM rationale stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_type: ReportType
    severity: Severity
    file: str | None = None
    line: int | None = None
    title: str
    description: str


class PrimitiveRiskSummary(BaseModel):
    """Per-primitive aggregated risk statistics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_type: ReportType
    total_findings: int = Field(ge=0)
    by_severity: dict[Severity, int]
    contributed_score: float = Field(ge=0.0)


class GateScore(BaseModel):
    """The overall risk score + recommendation from Stage B."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    score: float = Field(ge=0.0)
    recommendation: Recommendation
    threshold_pass: float = Field(ge=0.0)
    threshold_review: float = Field(ge=0.0)


class GatePolicy(BaseModel):
    """Policy loaded from .foundry/gate-policy.yaml.

    All fields have defaults matching the spec.
    """

    model_config = ConfigDict(extra="forbid")

    # severity_weight: low/medium/high/critical → float
    severity_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "low": 1.0,
            "medium": 4.0,
            "high": 10.0,
            "critical": 25.0,
        }
    )
    # confidence_weight: low/medium/high → float
    confidence_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "low": 0.3,
            "medium": 0.7,
            "high": 1.0,
        }
    )
    # primitive_weight: keyed by ReportType str → float
    primitive_weights: dict[str, float] = Field(default_factory=dict)
    # thresholds: "pass" and "review" keys
    thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "pass": 20.0,
            "review": 50.0,
        }
    )


class ConfidenceGateReport(BaseModel):
    """Full output of the C6 confidence-gate pipeline."""

    model_config = ConfigDict(extra="forbid")

    report_paths: list[str]
    primitive_summaries: list[PrimitiveRiskSummary]
    gate_score: GateScore
    rationale: str
    top_findings: list[TopFinding]
    generated_at: datetime

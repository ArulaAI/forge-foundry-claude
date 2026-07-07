"""C8 /architecture-review — Pydantic models.

All models use extra="forbid" (strict). AIQualityReport and datetime are kept
as runtime imports (noqa: TC001/TC003) so Pydantic can resolve field annotations.
No cycles between modules.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic field, runtime-required
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, field_validator

# Runtime import is safe: ai_quality_aggregator's only import from this module
# is itself TYPE_CHECKING-guarded, so there is no cycle.
from primitives.architecture_review.shared.ai_quality_aggregator import (  # noqa: TC001
    AIQualityReport,  # Pydantic field in ArchitectureReport, runtime-required
)

Language: TypeAlias = Literal["python", "java", "ts"]

SubjectiveIssueKind: TypeAlias = Literal["abstraction", "api_design", "cross_cutting_leak"]


class Module(BaseModel):
    """A source module in the dependency graph."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    path: str
    language: Language


class DepGraph(BaseModel):
    """Static dependency graph for a codebase.

    modules: list of discovered modules
    edges: directed dependency edges (from_module_name, to_module_name)
    """

    model_config = ConfigDict(extra="forbid")

    modules: list[Module]
    edges: list[tuple[str, str]]

    @property
    def module_names(self) -> set[str]:
        """Set of all module names in this graph."""
        return {m.name for m in self.modules}


class LayerViolation(BaseModel):
    """A dependency edge that violates the declared layer direction."""

    model_config = ConfigDict(extra="forbid")

    from_module: str
    to_module: str
    expected_direction: str  # e.g. "web -> service -> data"
    actual_direction: str  # e.g. "data -> web (reversed)"
    severity: Literal["low", "medium", "high", "critical"]
    source: Literal["c8_static", "c11_fm10"] = "c8_static"
    #: True when the edge crosses a trust boundary declared by S10
    #: (``.foundry/context/threat-model.json``). Defaults to False so
    #: behavior is identical to v0.11.0 when no upstream artifact exists.
    trust_boundary_violation: bool = False


class CouplingMetric(BaseModel):
    """Afferent/efferent coupling + instability for one module."""

    model_config = ConfigDict(extra="forbid")

    module: str
    afferent: int  # in-degree (how many modules import this)
    efferent: int  # out-degree (how many modules this imports)
    instability: float  # efferent / (afferent + efferent), 0.0 if both are 0

    @field_validator("instability")
    @classmethod
    def instability_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"instability must be in [0.0, 1.0], got {v}")
        return v


class CyclicDependency(BaseModel):
    """A strongly-connected component (cycle) of size >= 2."""

    model_config = ConfigDict(extra="forbid")

    modules: list[str]
    size: int

    _MIN_CYCLE_SIZE: int = 2

    @field_validator("size")
    @classmethod
    def size_at_least_two(cls, v: int) -> int:
        min_size = 2
        if v < min_size:
            raise ValueError(f"CyclicDependency.size must be >= 2, got {v}")
        return v


class SubjectiveIssue(BaseModel):
    """An issue identified by the LLM during Stage E subjective review."""

    model_config = ConfigDict(extra="forbid")

    kind: SubjectiveIssueKind
    module: str | None = None
    description: str
    severity: Literal["low", "medium", "high", "critical"]
    #: True when the issue's module touches a PII / PCI / PHI field
    #: classified by S60 (``.foundry/context/data-flow.json``). Defaults
    #: to False so behavior is identical to v0.11.0 when no upstream
    #: artifact exists.
    exposes_pii: bool = False


class ArchitectureReport(BaseModel):
    """Full output of the C8 architecture-review pipeline."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    code_path: str
    layer_violations: list[LayerViolation]
    coupling: dict[str, CouplingMetric]
    cycles: list[CyclicDependency]
    subjective: list[SubjectiveIssue]
    # Forward reference; AIQualityReport is concrete at runtime but defined in a sibling module.
    ai_quality: AIQualityReport | None = None
    generated_at: datetime

    @property
    def has_critical_ai_quality(self) -> bool:
        """True if sub-call C10/C11 returned any critical finding."""
        return self.ai_quality is not None and self.ai_quality.has_critical

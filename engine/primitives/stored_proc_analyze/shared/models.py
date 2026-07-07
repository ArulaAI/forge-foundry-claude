"""M2 /mod-stored-proc-analyze — Pydantic data models.

All ``Literal`` aliases are paired with an ``_ALL_*`` tuple so exhaustiveness
tests (and downstream consumers) can iterate the allowed values without
re-parsing the type expression. Every literal value MUST appear verbatim
in the system prompts (``shared/prompts.py``, lands with M2-7) that ask the
LLM to return it, mirroring the C1 prompt-vs-Literal guard used by the
other generative-stage primitives.

The model surface here matches the SPEC.md "File contract" + #204 issue
body: ``StoredProcDigest`` wraps ``list[StoredProc]``; each ``StoredProc``
carries its parameters, computed profile, IO shape, side-effect surface,
business-logic classification, and composite ``RiskScore``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from primitives._response_schema_validator import accepts_extended_schema

# ---------------------------------------------------------------------------
# IOShape
# ---------------------------------------------------------------------------

IOShape: TypeAlias = Literal[
    "reads_only",
    "reads_writes",
    "writes_only",
    "cursor_heavy",
    "cross_db_link",
]

_ALL_IO_SHAPES: tuple[IOShape, ...] = (
    "reads_only",
    "reads_writes",
    "writes_only",
    "cursor_heavy",
    "cross_db_link",
)

# ---------------------------------------------------------------------------
# SideEffect
# ---------------------------------------------------------------------------

SideEffect: TypeAlias = Literal[
    "email",
    "file",
    "external",
    "dynamic_sql",
    "none",
]

_ALL_SIDE_EFFECTS: tuple[SideEffect, ...] = (
    "email",
    "file",
    "external",
    "dynamic_sql",
    "none",
)

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

Classification: TypeAlias = Literal[
    "crud_wrapper",
    "validation",
    "calculation",
    "orchestration",
    "reporting",
    "etl",
    "trigger_handler",
    "unknown",
]

_ALL_CLASSIFICATIONS: tuple[Classification, ...] = (
    "crud_wrapper",
    "validation",
    "calculation",
    "orchestration",
    "reporting",
    "etl",
    "trigger_handler",
    "unknown",
)

# ---------------------------------------------------------------------------
# ParseStatus
# ---------------------------------------------------------------------------

ParseStatus: TypeAlias = Literal["parsed", "unparseable"]

_ALL_PARSE_STATUSES: tuple[ParseStatus, ...] = ("parsed", "unparseable")

# ---------------------------------------------------------------------------
# Param
# ---------------------------------------------------------------------------


class Param(BaseModel):
    """A single stored-procedure parameter.

    ``mode`` records the procedural-language calling convention (``IN`` /
    ``OUT`` / ``INOUT``) as documented in the SP source. ``type`` preserves
    the raw DDL type string (e.g. ``VARCHAR2(50)``, ``NUMBER(10,2)``,
    ``UNIQUEIDENTIFIER``) — the M-tier file contract is dialect-preserving
    so downstream M4 conversion plans can render targets faithfully.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str
    mode: Literal["IN", "OUT", "INOUT"] = "IN"
    default: str | None = None


# ---------------------------------------------------------------------------
# RiskScore
# ---------------------------------------------------------------------------


class RiskScore(BaseModel):
    """Composite conversion-risk score for a single stored procedure.

    Each component is a non-negative integer; ``total`` is the orchestrator-
    computed roll-up that downstream M3 sequencing and M4 plan synthesis
    sort against. Score weighting + calibration are documented alongside
    ``shared/risk_scorer.py`` (M2-9) — keeping the model unopinionated lets
    the scorer evolve without re-shaping the persisted digest.

    ``write_breadth`` (#886) captures write-surface breadth: a combination
    of ``table_touch_count`` (how many distinct tables the SP touches —
    broader data dependencies mean a higher migration surface) and an
    ``io_shape`` write-surface modifier (``reads_writes`` adds 1 on top of
    the table-count bucket because coordinated read+write patterns are
    riskier to migrate than pure-write patterns that only flow one
    direction). This axis differentiates the non-dynamic-SQL population so
    that a 6-table ETL procedure does not tie a trivial single-table CRUD
    wrapper at zero.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    composite: int = Field(ge=0)
    complexity: int = Field(ge=0)
    side_effects: int = Field(ge=0)
    cursor_depth: int = Field(ge=0)
    dynamic_sql: int = Field(ge=0)
    write_breadth: int = Field(ge=0)
    total: int = Field(ge=0)


# ---------------------------------------------------------------------------
# StoredProc
# ---------------------------------------------------------------------------


class StoredProc(BaseModel):
    """A single stored procedure / function / trigger profile."""

    model_config = ConfigDict(extra="forbid")

    name: str
    schema_: str = Field(alias="schema")
    params: list[Param] = Field(default_factory=list)
    language: str
    body_loc: int = Field(ge=0)
    cyclomatic: int = Field(ge=0)
    io_shape: IOShape
    side_effects: list[SideEffect] = Field(default_factory=list)
    classification: Classification
    risk_score: RiskScore
    touched_tables: list[str] = Field(default_factory=list)
    parse_status: ParseStatus


# ---------------------------------------------------------------------------
# StoredProcDigest
# ---------------------------------------------------------------------------


@accepts_extended_schema
class StoredProcDigest(BaseModel):
    """Top-level digest produced by the M2 pipeline.

    Schema-versioned so downstream primitives can refuse to consume a
    mismatched output (design spec §"File contract" rule). ``source_dialect``
    is a free-form string — sqlglot owns the dialect registry, see SPEC.md
    §"Dialect coverage" for rationale.

    Gemini subset (issue #548 / SPEC §6.3 + §7.2): persisted M2 artefact,
    not an LLM ``response_format`` schema. Pydantic emits ``anyOf`` for
    ``Optional`` ``Param.default`` and ``$ref`` chains for nested
    ``StoredProc``/``Param``/``RiskScore`` — both exceed Gemini's accepted
    subset. Decorated with ``@accepts_extended_schema`` because the digest
    is written to disk for downstream M3 consumption, not sent through
    structured-output mode.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    source_dialect: str
    procedures: list[StoredProc] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "Classification",
    "IOShape",
    "Param",
    "ParseStatus",
    "RiskScore",
    "SideEffect",
    "StoredProc",
    "StoredProcDigest",
]

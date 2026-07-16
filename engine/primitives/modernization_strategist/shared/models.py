"""M3 /mod-sql-strategist — Pydantic data models.

The decomposition surface M3's deterministic Stages A-D produce: bounded
contexts (Stage A), an ownership report (Stage B), the reused Layer-1
:class:`Roadmap` (Stage C, via the engine sequencer), and detected
antipatterns (Stage D), assembled into a single :class:`DecompositionPlan`.

All models are frozen / ``extra="forbid"`` and pure data (no LLM). The
persisted :class:`DecompositionPlan` is decorated with
``@accepts_extended_schema`` because its JSON Schema nests ``$ref`` chains
(``BoundedContext`` / ``OwnershipReport`` / ``Roadmap`` / ``Antipattern``)
beyond the Gemini ``response_format`` subset — it is written to disk for
downstream M4 consumption, never sent through structured-output mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from primitives._response_schema_validator import accepts_extended_schema
from primitives._shared.modernization.models import (
    Roadmap,  # noqa: TC001  (Pydantic needs it at runtime)
)

# ---------------------------------------------------------------------------
# AntipatternKind
# ---------------------------------------------------------------------------

AntipatternKind = Literal[
    "cross_context_fk",
    "distributed_transaction",
    "view_of_views",
    "multi_context_sp",
]

_ALL_ANTIPATTERN_KINDS: tuple[AntipatternKind, ...] = (
    "cross_context_fk",
    "distributed_transaction",
    "view_of_views",
    "multi_context_sp",
)


# ---------------------------------------------------------------------------
# BoundedContext (Stage A)
# ---------------------------------------------------------------------------


class BoundedContext(BaseModel):
    """A candidate bounded context — a cohesive cluster of tables.

    ``tables`` is a sorted tuple of table names; ``rationale`` is a short
    human-readable string explaining the cohesion signal(s) that grouped
    the cluster (FK / shared stored proc / shared LoB classification).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    tables: tuple[str, ...]
    rationale: str = ""


# ---------------------------------------------------------------------------
# OwnershipReport (Stage B)
# ---------------------------------------------------------------------------


class OwnershipReport(BaseModel):
    """Single-writer vs. multi-writer ownership across the schema.

    ``owned`` are tables with ≤1 distinct writer (a clean single-owner
    table). ``shared`` are tables written by ≥2 distinct procs, or written
    by a proc whose touched tables span more than one context (a
    cross-context shared write — a migration blocker). ``writers_by_table``
    maps every table to its sorted tuple of writer-proc names.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    owned: tuple[str, ...]
    shared: tuple[str, ...]
    writers_by_table: dict[str, tuple[str, ...]]


# ---------------------------------------------------------------------------
# Antipattern (Stage D)
# ---------------------------------------------------------------------------


class Antipattern(BaseModel):
    """A detected decomposition antipattern.

    ``kind`` is one of the four recognised structural smells; ``detail`` is
    a precise human-readable description; ``evidence`` names the objects
    (tables / procs / views / FK constraints) involved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: AntipatternKind
    detail: str
    evidence: tuple[str, ...]


# ---------------------------------------------------------------------------
# DecompositionPlan (orchestrator output)
# ---------------------------------------------------------------------------


@accepts_extended_schema
class DecompositionPlan(BaseModel):
    """Top-level deterministic decomposition produced by Stages A-D.

    Schema-versioned so downstream M4 can refuse a mismatched output. The
    JSON Schema nests ``$ref`` chains, so the model is exempted from the
    Gemini ``response_format`` subset (``@accepts_extended_schema``); it is
    a persisted machine artefact, not an LLM response schema.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    contexts: list[BoundedContext] = Field(default_factory=list)
    ownership: OwnershipReport
    roadmap: Roadmap
    antipatterns: list[Antipattern] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "Antipattern",
    "AntipatternKind",
    "BoundedContext",
    "DecompositionPlan",
    "OwnershipReport",
]

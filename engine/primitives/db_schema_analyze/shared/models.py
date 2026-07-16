"""M1 /mod-db-schema-analyze — Pydantic data models.

The model surface here matches the SPEC.md "File contract" + #188 issue
body: ``SchemaDigest`` wraps ``list[Table] + list[View] + list[Sequence]``
plus the dependency-DAG edge list; each ``Table`` carries its columns,
primary key, foreign keys, indexes, LoB classification, row-count
estimate (best-effort) and the hotspot / orphan flags computed in
Stage E.

All ``Literal`` aliases are paired with an ``_ALL_*`` tuple so
exhaustiveness tests (and downstream consumers) can iterate the allowed
values without re-parsing the type expression. Every literal value MUST
appear verbatim in the Stage D prompts (``shared/prompts.py``, lands
with M1-9) that ask the LLM to return it, mirroring the M2 pattern.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from primitives._response_schema_validator import accepts_extended_schema

# ---------------------------------------------------------------------------
# PIIClassification — S60 cross-reference
# ---------------------------------------------------------------------------

PIIClassification: TypeAlias = Literal["pii", "sensitive", "public", "unknown"]

_ALL_PII_CLASSIFICATIONS: tuple[PIIClassification, ...] = (
    "pii",
    "sensitive",
    "public",
    "unknown",
)

# ---------------------------------------------------------------------------
# OnDeleteAction — FK referential-action enum
# ---------------------------------------------------------------------------

OnDeleteAction: TypeAlias = Literal["cascade", "set_null", "restrict", "no_action"]

_ALL_ON_DELETE_ACTIONS: tuple[OnDeleteAction, ...] = (
    "cascade",
    "set_null",
    "restrict",
    "no_action",
)

# ---------------------------------------------------------------------------
# IndexKind — clustered / non-clustered / unique / bitmap / spatial / fulltext
# ---------------------------------------------------------------------------

IndexKind: TypeAlias = Literal[
    "btree",
    "bitmap",
    "hash",
    "fulltext",
    "spatial",
    "clustered",
    "nonclustered",
    "unknown",
]

_ALL_INDEX_KINDS: tuple[IndexKind, ...] = (
    "btree",
    "bitmap",
    "hash",
    "fulltext",
    "spatial",
    "clustered",
    "nonclustered",
    "unknown",
)

# ---------------------------------------------------------------------------
# Column
# ---------------------------------------------------------------------------


class Column(BaseModel):
    """A single column on a table.

    ``type`` preserves the raw DDL type string (e.g. ``VARCHAR2(50)``,
    ``NUMBER(10,2)``, ``UNIQUEIDENTIFIER``) — the M-tier file contract is
    dialect-preserving so downstream M3 / M4 / S60 can reason about the
    legacy schema without re-parsing the source DDL.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str
    nullable: bool
    default: str | None = None
    pii_classification: PIIClassification | None = None


# ---------------------------------------------------------------------------
# ForeignKey
# ---------------------------------------------------------------------------


class ForeignKey(BaseModel):
    """A single foreign-key constraint on a table.

    Multi-column FKs are supported via the parallel ``columns`` /
    ``referenced_columns`` lists; their lengths must match (validated at
    Stage B in ``shared/object_inventory.py``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    constraint_name: str
    columns: list[str]
    referenced_table: str
    referenced_columns: list[str]
    on_delete: OnDeleteAction | None = None


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


class Index(BaseModel):
    """A single index on a table.

    ``kind`` is dialect-normalised to the union of recognised index types
    across the five shipped dialects (Oracle, T-SQL, DB2, PostgreSQL,
    MySQL). Dialect-specific index attributes that do not normalise are
    captured by ``unknown`` so the digest still records the presence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    columns: list[str]
    unique: bool
    kind: IndexKind = "unknown"


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class View(BaseModel):
    """A database view.

    ``definition_sql`` preserves the original ``CREATE VIEW`` body so
    Stage C can build the view-reference dependency DAG without
    re-walking the AST, and so M3 can audit view-shaped logic when
    sequencing a strangler-fig decomposition.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    schema_: str = Field(alias="schema")
    definition_sql: str
    referenced_tables: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Sequence
# ---------------------------------------------------------------------------


class Sequence(BaseModel):
    """A database sequence (Oracle ``SEQUENCE`` / T-SQL ``SEQUENCE`` /
    DB2 ``SEQUENCE`` / PostgreSQL ``SEQUENCE`` — MySQL omits but ships
    AUTO_INCREMENT semantics which Stage B normalises into a Sequence
    record for downstream consistency).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    schema_: str = Field(alias="schema")
    start: int
    increment: int
    cache_size: int | None = None


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


class Table(BaseModel):
    """A single table profile.

    Carries enough context for M2 / M3 / M4 to operate without re-reading
    the original DDL source: columns (with ``pii_classification`` for S60
    cross-reference), primary key, foreign keys, indexes, line-of-business
    classification (Stage D), row-count estimate (best-effort from
    ``ANALYZE``-style hints in the DDL), and the hotspot / orphan flags
    computed in Stage E.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    schema_: str = Field(alias="schema")
    columns: list[Column]
    primary_key: list[str] = Field(default_factory=list)
    foreign_keys: list[ForeignKey] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)
    lob_classification: str | None = None
    row_count_estimate: int | None = Field(default=None, ge=0)
    is_hotspot: bool = False
    is_orphan: bool = False


# ---------------------------------------------------------------------------
# SchemaDigest
# ---------------------------------------------------------------------------


@accepts_extended_schema
class SchemaDigest(BaseModel):
    """Top-level digest produced by the M1 pipeline.

    Schema-versioned so downstream primitives can refuse to consume a
    mismatched output (design spec §"File contract" rule).
    ``source_dialect`` is a free-form string — sqlglot owns the dialect
    registry, matching the M2 ``StoredProcDigest`` pattern.

    ``dependency_dag_edges`` is the list of directed FK / view-reference
    edges Stage C extracted. Each edge is a ``(from_table, to_table)``
    pair encoded as a tuple; the ``schema.foreign_keys`` field on each
    ``Table`` carries the same information at the constraint level, but
    a flat edge list makes Stage E's hotspot scoring and M3's
    bounded-context discovery cheap.

    ``foundry_primitive_versions`` records the version of every foundry
    primitive that contributed to the digest, for provenance — design
    spec §"File contract" requirement.

    Gemini subset (issue #548 / SPEC §6.3 + §7.2): this is a canonical
    persisted artefact, not an LLM ``response_format`` schema. Pydantic
    emits ``anyOf`` for ``Optional[...]`` columns + ``$ref`` chains for
    nested ``Table``/``Column`` models — both exceed Gemini's
    ``responseSchema`` subset. Decorated with ``@accepts_extended_schema``
    so the SchemaDigest is NOT routed through harness structured-output
    mode. If a future PR ever wires SchemaDigest through ``response_format``,
    the schema must first be flattened (drop ``$defs``/``$ref`` indirection,
    replace ``Optional`` with ``nullable``) or the call site must explicitly
    catch ``HarnessUnsupportedError`` and fall back to free-form parsing.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    source_dialect: str
    tables: list[Table] = Field(default_factory=list)
    views: list[View] = Field(default_factory=list)
    sequences: list[Sequence] = Field(default_factory=list)
    dependency_dag_edges: list[tuple[str, str]] = Field(default_factory=list)
    cycles: list[list[str]] = Field(
        default_factory=list,
        description=(
            "Dependency cycles in the schema graph. Each inner list is the sorted "
            "qualified table names of one strongly-connected component (size >= 2) "
            "or a single-node self-loop. Populated by Stage C alongside "
            "``dependency_dag_edges``; the outer list is sorted by each cycle's "
            "lexicographically smallest member."
        ),
    )
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    foundry_primitive_versions: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "Column",
    "ForeignKey",
    "Index",
    "IndexKind",
    "OnDeleteAction",
    "PIIClassification",
    "SchemaDigest",
    "Sequence",
    "Table",
    "View",
]

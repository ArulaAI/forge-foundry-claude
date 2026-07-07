"""Mainframe-specific record models (CopybookRecord, CICSTransaction, etc.).

These are the concrete mainframe-vocabulary classes retained for backwards
compatibility alongside the abstract entity vocabulary in ``entities.py``.
Existing analyser / renderer / auditor code keeps importing them and
constructing them with the mainframe-flavoured field names.

Must NOT import from ``digest``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from primitives._shared.discovery.models.entities import SourceLanguage  # noqa: TC001

# ---------------------------------------------------------------------------
# CopybookRecord
# ---------------------------------------------------------------------------


class CopybookRecord(BaseModel):
    """Copybook metadata extracted by Phase B.

    ``fields`` is the ordered list of 01/05-level field names declared in
    the copybook. ``record_layout`` preserves the raw copybook body so the
    Phase C renderers + M6 entity templating can reproduce field-by-field
    references without re-parsing. ``used_by_programs`` is the back-pointer
    (program names that ``COPY`` this copybook) — the forward edge lives on
    ``ProgramRecord.copybook_deps``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    fields: list[str] = Field(default_factory=list)
    record_layout: str = ""
    used_by_programs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# CICSTransaction
# ---------------------------------------------------------------------------


class CICSTransaction(BaseModel):
    """CICS transaction extracted from the CSD or the source tree.

    ``transid`` is the 4-character CICS transaction identifier (e.g.
    ``BANK``, ``ACCT``). ``target_program`` is the program name CSD maps
    the TRANSID to. ``bms_maps`` is the list of BMS mapset / map names
    the transaction's program references via ``EXEC CICS SEND MAP``.
    ``security_requirements`` is a free-form list of security / auth
    classes (``RACF``, ``transaction_security``, …) — Phase B leaves
    this open-ended to avoid pinning a Literal that the consumer site
    cannot extend.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    transid: str
    target_program: str
    bms_maps: list[str] = Field(default_factory=list)
    security_requirements: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# VSAMFileRecord
# ---------------------------------------------------------------------------


class VSAMFileRecord(BaseModel):
    """VSAM file metadata + access-mode matrix.

    ``record_layout`` carries the copybook name (or inline layout
    description) the application uses against this VSAM file.
    ``accessed_by`` is the access-mode matrix: keyed by program name,
    values are the set of CICS access modes (``READ`` / ``WRITE`` /
    ``REWRITE`` / ``DELETE`` / ``BROWSE``) that program performs. The
    matrix is left open-form (``list[str]``) because dialect coverage
    differs across IBM CICS / Micro Focus / Tmaxsoft.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    record_layout: str = ""
    accessed_by: dict[str, list[str]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# BMSMapRecord
# ---------------------------------------------------------------------------


class BMSMapRecord(BaseModel):
    """BMS mapset metadata.

    ``mapset`` is the BMS mapset name (e.g. ``BANKMSET``). ``maps`` is
    the list of map names defined inside the mapset. ``terminal_screens``
    is the curated list of screen labels Phase B extracts from the
    mapset for §7 (Screen Navigation & UI Layer) rendering. ``fields``
    is the flat list of named fields across all maps — uniqueness is
    not enforced here because the same field name can legitimately
    appear on multiple maps.
    """

    model_config = ConfigDict(extra="forbid")

    mapset: str
    maps: list[str] = Field(default_factory=list)
    terminal_screens: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# JCLJobRecord
# ---------------------------------------------------------------------------


class JCLJobRecord(BaseModel):
    """JCL batch job metadata.

    ``steps`` is the ordered list of step names in the job. The flat
    ``programs_invoked`` list collapses every ``EXEC PGM=`` reference
    across steps so §9 (Batch Processing) can summarise quickly;
    ``files_read`` / ``files_written`` carry the DD-statement file
    accesses, used by the VSAM cross-reference + the Phase D
    entity-mismatch auditor.
    """

    model_config = ConfigDict(extra="forbid")

    job_name: str
    steps: list[str] = Field(default_factory=list)
    programs_invoked: list[str] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_written: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ProgramRecord
# ---------------------------------------------------------------------------


class ProgramRecord(BaseModel):
    """A single mainframe program profile (Phase B output).

    Carries enough context for §3 (Program Inventory), §5 (Call Chain
    Maps), §10 (Security Architecture), and the optional C8 sub-call to
    operate without re-parsing source. ``language`` is one of the
    ``SourceLanguage`` literals (``cobol`` / ``pli`` / ``asm`` are the
    common mainframe-program values, but the field is typed as the
    superset so Phase A's LoC counter can record any source file the
    tree happens to contain).

    ``copybook_deps`` lists ``COPY`` targets declared in source.
    ``cics_transactions`` is the inverse of ``CICSTransaction.
    target_program`` (TRANSIDs that route to this program).
    ``called_by`` / ``calls_to`` are the per-program call-chain edges
    Stage B extracts from ``EXEC CICS LINK`` / ``EXEC CICS XCTL`` /
    ``CALL`` statements. ``complexity`` is a best-effort metric (e.g.
    cyclomatic from a heuristic walk) — left optional so non-CICS
    flavors (M5 v1.5+) can omit it when the parser is a stub.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    language: SourceLanguage
    source_path: str
    copybook_deps: list[str] = Field(default_factory=list)
    cics_transactions: list[str] = Field(default_factory=list)
    called_by: list[str] = Field(default_factory=list)
    calls_to: list[str] = Field(default_factory=list)
    line_count: int = Field(default=0, ge=0)
    complexity: int | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# ProgramInventoryEntry + ProgramInventory — narrower shapes for optional C8
# ---------------------------------------------------------------------------


class ProgramInventoryEntry(BaseModel):
    """Single entry in the ``ProgramInventory`` shape produced by
    ``--sub-call-c8`` (design spec §"Sub-call rules").

    Deliberately narrower than ``ProgramRecord``: C8's
    ``/architecture-review`` consumer needs the *shape* of the program
    inventory + the call-chain edges, not every field. The narrow shape
    keeps the C8 prompt budget bounded and makes the dedup discipline
    (M3 → C8 pattern) cheap.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    language: SourceLanguage
    calls_to: list[str] = Field(default_factory=list)
    called_by: list[str] = Field(default_factory=list)


class ProgramInventory(BaseModel):
    """Sub-call payload for the optional ``--sub-call-c8`` opt-in.

    Schema-versioned at ``"1.0"`` so the C8 architectural-review
    consumer can refuse a mismatched payload. ``entries`` is the
    deliberately narrow ``ProgramInventoryEntry`` list (not the full
    ``ProgramRecord``) — keep the C8 prompt budget bounded.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    code_root: str
    entries: list[ProgramInventoryEntry] = Field(default_factory=list)


__all__ = [
    "BMSMapRecord",
    "CICSTransaction",
    "CopybookRecord",
    "JCLJobRecord",
    "ProgramInventory",
    "ProgramInventoryEntry",
    "ProgramRecord",
    "VSAMFileRecord",
]

"""Shared Literal TypeAliases, exhaustiveness tuples, and the six core entity BaseModels.

This is the base sub-module of the discovery models package.  It must NOT
import from ``legacy`` or ``digest``.
"""

from __future__ import annotations

from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# PlatformFlavor — closed Literal mirroring the ``--platform-flavor`` CLI flag
# ---------------------------------------------------------------------------
#
# M-EPIC-3 (#520) reframes M5 as a polyglot discovery primitive; the CLI
# flag accordingly renamed ``--mainframe-flavor`` → ``--platform-flavor``
# (M5-RENAME-FLAG / #523). The model surface follows: ``PlatformFlavor``
# is the canonical name going forward; ``MainframeFlavor`` is retained as
# a backwards-compat ``TypeAlias`` so the v1.0.x deprecation window can
# carry existing consumers. New non-mainframe flavor values land via the
# entry-point registry extension (M5-REGISTRY-EXTENSION / #524), NOT here
# — this rename is surface-only.

PlatformFlavor: TypeAlias = Literal[
    # M5-FLAVOR-1 + M5-FLAVOR-2 built-in mainframe trio
    "cics_cobol",
    "jcl_batch",
    "ims_db",
    # M5-FLAVOR-PYTHON (#526) — first non-mainframe built-in. Full
    # AST-based parser; see ``shared/flavor/python.py``.
    "python",
    # M5-FLAVOR-JAVA-EE (#527) — second non-mainframe full built-in.
    # Regex-based parser; see ``shared/flavor/java_ee.py``.
    "java_ee",
    # M5-FLAVOR-STUBS (#528) polyglot stub quintet — parser
    # ``NotImplementedError`` raises until each lands as a sub-issue
    # under EPIC #520. Listed in the closed Literal so
    # ``--platform-flavor <stub>`` type-checks at the model boundary
    # and the audit reports a typed flavor selection (rather than
    # falling through to a string-only path).
    "powerbuilder",
    "delphi",
    "vb6",
    "oracle_forms",
    "as400_rpg",
]

# Backwards-compat alias. Existing callers importing ``MainframeFlavor``
# continue to type-check (and round-trip via the Pydantic Literal). The
# alias is preserved for v1.0.x per the design spec §"Backwards
# compatibility"; removal is gated on a v2.0.0 bump. The alias also
# covers the new ``"python"`` value (M5-FLAVOR-PYTHON / #526) — the
# ``MainframeFlavor`` name is now a misnomer for the polyglot reality
# but ``PlatformFlavor`` is the canonical name going forward.
MainframeFlavor: TypeAlias = PlatformFlavor

_ALL_PLATFORM_FLAVORS: tuple[PlatformFlavor, ...] = (
    "cics_cobol",
    "jcl_batch",
    "ims_db",
    "python",
    "java_ee",
    "powerbuilder",
    "delphi",
    "vb6",
    "oracle_forms",
    "as400_rpg",
)

# Backwards-compat alias for the exhaustiveness tuple. Mirrors the
# ``MainframeFlavor = PlatformFlavor`` alias above so downstream tests
# and consumers iterating over ``_ALL_MAINFRAME_FLAVORS`` keep working.
_ALL_MAINFRAME_FLAVORS: tuple[MainframeFlavor, ...] = _ALL_PLATFORM_FLAVORS

# ---------------------------------------------------------------------------
# SourceLanguage — closed Literal of the per-file LoC bucket keys Phase A
# reports against. ``other`` is the catch-all for non-mainframe files that
# show up in the source tree (READMEs, build scripts, …).
# ---------------------------------------------------------------------------

SourceLanguage: TypeAlias = Literal[
    "cobol",
    "copybook",
    "pli",
    "jcl",
    "bms",
    "asm",
    "sql",
    "python",
    "other",
]

_ALL_SOURCE_LANGUAGES: tuple[SourceLanguage, ...] = (
    "cobol",
    "copybook",
    "pli",
    "jcl",
    "bms",
    "asm",
    "sql",
    "python",
    "other",
)

# ---------------------------------------------------------------------------
# Confidence — closed Literal carried on every ``EvidenceRow``. The three
# levels mirror the BankDemo reference's "Zero Hallucination Protocol"
# ladder: ``high`` = two-or-more evidence sources cross-validated;
# ``medium`` = single-source claim retained because the renderer needs it;
# ``low`` = retained-but-flagged (renders as ``⚠️ UNVERIFIED`` in the
# playbook).
# ---------------------------------------------------------------------------

Confidence: TypeAlias = Literal["high", "medium", "low"]

_ALL_CONFIDENCES: tuple[Confidence, ...] = ("high", "medium", "low")

# ---------------------------------------------------------------------------
# AuditFindingKind — closed Literal enumerating the Phase D check
# families documented in the design spec §"Phase D — Self-Audit" matrix.
# Five families after #862 extended the set with synthesis-citation
# refchecking.
# ---------------------------------------------------------------------------

AuditFindingKind: TypeAlias = Literal[
    "citation_missing",
    "entity_mismatch",
    "mermaid_node_orphan",
    "section_missing",
    "synthesis_citation_unresolved",
]

_ALL_AUDIT_FINDING_KINDS: tuple[AuditFindingKind, ...] = (
    "citation_missing",
    "entity_mismatch",
    "mermaid_node_orphan",
    "section_missing",
    "synthesis_citation_unresolved",
)

# ---------------------------------------------------------------------------
# Severity — closed Literal shared with M3 ``Antipattern.severity``. Kept
# co-located here so the M5 surface is self-contained for downstream
# consumers that import only M5 (e.g. M6's ingestion module).
# ---------------------------------------------------------------------------

Severity: TypeAlias = Literal["low", "medium", "high", "critical"]

_ALL_SEVERITIES: tuple[Severity, ...] = ("low", "medium", "high", "critical")


# ===========================================================================
# Abstract entity vocabulary (M-EPIC-3 / #520; M5-MODELS-V2 / #522)
# ===========================================================================
#
# Each abstract type carries a closed-Literal ``kind`` so per-platform
# flavour packs can route records into ``AnalysisDigest`` without the
# downstream Phase C renderer or Phase D auditor having to switch on
# concrete class. The closed-Literal ``_ALL_*`` exhaustiveness tuple
# mirrors the M1 / M2 / M3 / M4 prompt-vs-Literal guard pattern.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ExecutionUnitKind — closed Literal for ``ExecutionUnit.kind``
# ---------------------------------------------------------------------------

ExecutionUnitKind: TypeAlias = Literal[
    "program",
    "module",
    "function",
    "class",
    "route_handler",
    "lambda",
    "rpg_program",
    "powerbuilder_userobject",
]

_ALL_EXECUTION_UNIT_KINDS: tuple[ExecutionUnitKind, ...] = (
    "program",
    "module",
    "function",
    "class",
    "route_handler",
    "lambda",
    "rpg_program",
    "powerbuilder_userobject",
)

# ---------------------------------------------------------------------------
# SharedComponentKind — closed Literal for ``SharedComponent.kind``
# ---------------------------------------------------------------------------

SharedComponentKind: TypeAlias = Literal[
    "copybook",
    "module",
    "package",
    "library",
]

_ALL_SHARED_COMPONENT_KINDS: tuple[SharedComponentKind, ...] = (
    "copybook",
    "module",
    "package",
    "library",
)

# ---------------------------------------------------------------------------
# EntryPointKind — closed Literal for ``EntryPoint.kind``
# ---------------------------------------------------------------------------

EntryPointKind: TypeAlias = Literal[
    "cics_transaction",
    "api_route",
    "cli_command",
    "scheduled_trigger",
    "web_route",
    # Track B (#1427) — the four unified entry-point classes broadened onto the
    # closed Literal so every inbound trigger (batch job, scheduled/cron task,
    # CLI entry, message consumer, external integration / webhook / SOAP) is
    # captured with the same shape. Additive — the historical values above stay.
    "batch",
    "scheduled",
    "cli",
    "messaging",
    "integration",
    "webhook",
    "soap",
]

_ALL_ENTRY_POINT_KINDS: tuple[EntryPointKind, ...] = (
    "cics_transaction",
    "api_route",
    "cli_command",
    "scheduled_trigger",
    "web_route",
    "batch",
    "scheduled",
    "cli",
    "messaging",
    "integration",
    "webhook",
    "soap",
)

# ---------------------------------------------------------------------------
# DataStoreKind — closed Literal for ``DataStore.kind``
# ---------------------------------------------------------------------------

DataStoreKind: TypeAlias = Literal[
    "vsam_file",
    "db_table",
    "cache",
    "queue",
    "object_store",
]

_ALL_DATA_STORE_KINDS: tuple[DataStoreKind, ...] = (
    "vsam_file",
    "db_table",
    "cache",
    "queue",
    "object_store",
)

# ---------------------------------------------------------------------------
# AccessMode — closed Literal for ``DataStore.accessed_by`` set values
# ---------------------------------------------------------------------------

AccessMode: TypeAlias = Literal["read", "write", "rewrite", "delete"]

_ALL_ACCESS_MODES: tuple[AccessMode, ...] = ("read", "write", "rewrite", "delete")

# ---------------------------------------------------------------------------
# UIElementKind — closed Literal for ``UIElement.kind``
# ---------------------------------------------------------------------------

UIElementKind: TypeAlias = Literal[
    "bms_map",
    "web_template",
    "react_component",
    "mobile_screen",
]

_ALL_UI_ELEMENT_KINDS: tuple[UIElementKind, ...] = (
    "bms_map",
    "web_template",
    "react_component",
    "mobile_screen",
)

# ---------------------------------------------------------------------------
# BatchUnitKind — closed Literal for ``BatchUnit.kind``
# ---------------------------------------------------------------------------

BatchUnitKind: TypeAlias = Literal[
    "jcl_job",
    "cron_task",
    "celery_beat",
    "scheduled_workflow",
]

_ALL_BATCH_UNIT_KINDS: tuple[BatchUnitKind, ...] = (
    "jcl_job",
    "cron_task",
    "celery_beat",
    "scheduled_workflow",
)


# ---------------------------------------------------------------------------
# ExecutionUnit — abstract over ProgramRecord
# ---------------------------------------------------------------------------


class ExecutionUnit(BaseModel):
    """Abstract execution-unit profile (M-EPIC-3 polyglot vocabulary).

    Mainframe-flavour projection of the abstract surface: a COBOL
    program is one ``ExecutionUnit`` with ``kind="program"`` and
    ``language="cobol"``. Python projection: a module is one
    ``ExecutionUnit`` with ``kind="module"``. Java projection: a class
    is one ``ExecutionUnit`` with ``kind="class"``.

    ``shared_component_deps`` is the polyglot analogue of
    ``ProgramRecord.copybook_deps`` — names of ``SharedComponent``
    records this unit imports / copies. ``called_by`` / ``calls_to``
    are call-chain edges in the per-flavour vocabulary (CICS LINK /
    Python import / Java method invocation / Node require).

    ``loc`` is the file's source line count — the polyglot rename of
    the mainframe-flavoured ``line_count`` field on ``ProgramRecord``.
    ``complexity`` is best-effort (cyclomatic-equivalent or stub) and
    optional so flavour packs that ship without a complexity heuristic
    can omit it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    source_path: str
    language: SourceLanguage
    kind: ExecutionUnitKind
    loc: int = Field(default=0, ge=0)
    complexity: int | None = Field(default=None, ge=0)
    called_by: list[str] = Field(default_factory=list)
    calls_to: list[str] = Field(default_factory=list)
    shared_component_deps: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SharedComponent — abstract over CopybookRecord
# ---------------------------------------------------------------------------


class SharedComponent(BaseModel):
    """Abstract shared-component profile (M-EPIC-3 polyglot vocabulary).

    Mainframe projection: a copybook is a ``SharedComponent`` with
    ``kind="copybook"``. Python projection: a shared utility module
    imported by ≥2 callers becomes ``kind="module"``. Java projection:
    a common-JAR package becomes ``kind="package"`` or ``kind="library"``.

    ``field_definitions`` is the mainframe-flavoured carryover (ordered
    list of 01/05-level field names from a copybook) — optional so
    non-mainframe flavours that have no field-definition concept can
    omit it without polluting the schema. ``used_by_units`` is the
    back-pointer (polyglot rename of ``CopybookRecord.used_by_programs``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: SharedComponentKind
    source_path: str = ""
    used_by_units: list[str] = Field(default_factory=list)
    field_definitions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# EntryPoint — abstract over CICSTransaction
# ---------------------------------------------------------------------------


class EntryPoint(BaseModel):
    """Abstract entry-point profile (M-EPIC-3 polyglot vocabulary).

    The thing an outside actor invokes. Mainframe projection: a CICS
    transaction is an ``EntryPoint`` with ``kind="cics_transaction"``
    targeting a COBOL program. Python projection: a ``@app.get`` route
    is ``kind="api_route"`` targeting the handler ``ExecutionUnit``.
    Node projection: an Express route is ``kind="web_route"``.

    ``target_unit`` names the ``ExecutionUnit`` the entry point dispatches
    into (the polyglot rename of ``CICSTransaction.target_program``).
    ``security_requirements`` and ``ui_elements`` carry forward
    open-ended so consumers can extend without a Literal edit per
    flavour.

    Track B (#1427): ``trigger`` carries the **real** inbound value — the cron
    expression for a ``scheduled`` job, the topic / queue for a ``messaging``
    consumer, the route path for an ``api_route`` / ``webhook``, the command for
    a ``cli`` entry. ``source`` is the ``file:line`` provenance of the
    declaration. Both default empty (the frozen model stays backward-compatible;
    the historical HTTP/CICS records simply carry ``""``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: EntryPointKind
    target_unit: str = ""
    trigger: str = ""
    source: str = ""
    security_requirements: list[str] = Field(default_factory=list)
    ui_elements: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# DataStore — abstract over VSAMFileRecord
# ---------------------------------------------------------------------------


class DataStore(BaseModel):
    """Abstract data-store profile (M-EPIC-3 polyglot vocabulary).

    A persistent or quasi-persistent place data lives. Mainframe
    projection: a VSAM file is a ``DataStore`` with ``kind="vsam_file"``.
    Python projection: a SQLAlchemy table maps to ``kind="db_table"``;
    a Redis key prefix to ``kind="cache"``; an SQS queue to
    ``kind="queue"``; an S3 prefix to ``kind="object_store"``.

    ``accessed_by`` is the access-mode matrix keyed by ``ExecutionUnit``
    name; values are *sets* of closed ``AccessMode`` Literal values
    (``read`` / ``write`` / ``rewrite`` / ``delete``). The set semantics
    (de-dup, order-insensitive) is the polyglot improvement over
    ``VSAMFileRecord.accessed_by`` which used open-form ``list[str]``
    because the as-shipped CICS dialect coverage varied across vendors.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: DataStoreKind
    accessed_by: dict[str, set[AccessMode]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# UIElement — abstract over BMSMapRecord
# ---------------------------------------------------------------------------


class UIElement(BaseModel):
    """Abstract UI-element profile (M-EPIC-3 polyglot vocabulary).

    Mainframe projection: a BMS map is a ``UIElement`` with
    ``kind="bms_map"``. Python projection: a Jinja2 template is
    ``kind="web_template"``. JavaScript projection: a React component
    file is ``kind="react_component"``. Mobile projection: an
    iOS/Android screen is ``kind="mobile_screen"``.

    ``referenced_by_units`` names the ``ExecutionUnit`` records that
    render or transition to this UI element. ``fields`` is the
    mainframe-flavoured carryover (flat list of named fields across
    the map) — optional so web-template / React flavours that have no
    flat-fields concept can omit it.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: UIElementKind
    referenced_by_units: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# BatchUnit — abstract over JCLJobRecord
# ---------------------------------------------------------------------------


class BatchUnit(BaseModel):
    """Abstract batch-unit profile (M-EPIC-3 polyglot vocabulary).

    A unit of work that runs without an interactive caller. Mainframe
    projection: a JCL job is a ``BatchUnit`` with ``kind="jcl_job"``.
    Python projection: a Celery beat schedule is ``kind="celery_beat"``;
    a generic cron entry is ``kind="cron_task"``. CI / CD projection:
    a scheduled GitHub Actions workflow is ``kind="scheduled_workflow"``.

    ``steps`` is the ordered list of ``(unit_name, args_summary)`` tuples
    — names the ``ExecutionUnit`` invoked at each step plus a short
    human-readable summary of the arguments (DD-statement summary for
    JCL; cron expression for cron_task; etc.). ``files_read`` /
    ``files_written`` carry forward as flat path lists; consumers
    cross-check these against ``DataStore.name`` for the polyglot
    rewrite of ``JCLJobRecord``'s VSAM-file cross-reference pass.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: BatchUnitKind
    source_path: str = ""  # repo-relative descriptor path (e.g. ops/k8s/billing-cronjob.yaml)
    steps: list[tuple[str, str]] = Field(default_factory=list)
    files_read: list[str] = Field(default_factory=list)
    files_written: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# DeploymentUnitKind — closed Literal for ``DeploymentUnit.kind``
# ---------------------------------------------------------------------------

DeploymentUnitKind: TypeAlias = Literal[
    "deployment",
    "statefulset",
    "daemonset",
    "replicaset",
    "job",
    "service",
    "ingress",
    "pod",
]

_ALL_DEPLOYMENT_UNIT_KINDS: tuple[DeploymentUnitKind, ...] = (
    "deployment",
    "statefulset",
    "daemonset",
    "replicaset",
    "job",
    "service",
    "ingress",
    "pod",
)


class DeploymentUnit(BaseModel):
    """A deployment-topology object discovered in an infrastructure manifest.

    Polyglot deployment vocabulary (#949): a Kubernetes ``Deployment`` /
    ``StatefulSet`` / ``Service`` / … manifest is one ``DeploymentUnit`` carrying
    its ``metadata.name``, lowercase ``kind``, and the repo-relative
    ``source_path`` (with the ``kind:`` line) so §11 Deployment Architecture can
    render the real topology instead of an ``⚠️ UNVERIFIED`` stub. ``replicas``
    and ``image`` are best-effort and optional (omitted when not cheaply
    extractable). Populated by a dedicated manifest scan, not the per-stack
    flavor's six-category ``parse()`` contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: DeploymentUnitKind
    source_path: str = ""
    replicas: int | None = Field(default=None, ge=0)
    image: str | None = None


__all__ = [
    "_ALL_ACCESS_MODES",
    "_ALL_AUDIT_FINDING_KINDS",
    "_ALL_BATCH_UNIT_KINDS",
    "_ALL_CONFIDENCES",
    "_ALL_DATA_STORE_KINDS",
    "_ALL_DEPLOYMENT_UNIT_KINDS",
    "_ALL_ENTRY_POINT_KINDS",
    "_ALL_EXECUTION_UNIT_KINDS",
    "_ALL_MAINFRAME_FLAVORS",
    "_ALL_PLATFORM_FLAVORS",
    "_ALL_SEVERITIES",
    "_ALL_SHARED_COMPONENT_KINDS",
    "_ALL_SOURCE_LANGUAGES",
    "_ALL_UI_ELEMENT_KINDS",
    "AccessMode",
    "AuditFindingKind",
    "BatchUnit",
    "BatchUnitKind",
    "Confidence",
    "DataStore",
    "DataStoreKind",
    "DeploymentUnit",
    "DeploymentUnitKind",
    "EntryPoint",
    "EntryPointKind",
    "ExecutionUnit",
    "ExecutionUnitKind",
    "MainframeFlavor",
    "PlatformFlavor",
    "Severity",
    "SharedComponent",
    "SharedComponentKind",
    "SourceLanguage",
    "UIElement",
    "UIElementKind",
]

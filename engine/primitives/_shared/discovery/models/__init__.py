"""M5 /mod-playbook-discovery — Pydantic data models.

The model surface here matches the SPEC.md "Output contract" + #443 issue
body. The M5 pipeline emits four canonical artefacts under ``--state-dir``:

* ``recon.json`` — Phase A output (``ReconReport``)
* ``analysis.json`` — Phase B aggregated output (``AnalysisDigest``
  wrapping the per-entity records below)
* ``playbook-state.json`` — phase / section checkpoint for ``--resume``
  (``PlaybookState``)
* ``evidence-ledger.jsonl`` — append-only ledger of every ``path:line``
  citation (``EvidenceRow``)

Phase D's audit findings stream out as ``list[AuditFinding]`` appended
to the §Validation Report at the bottom of the emitted ``PLAYBOOK.md``.

The optional ``--sub-call-c8`` opt-in (design spec §"Sub-call rules")
also produces a *narrower* ``ProgramInventory`` shape — a deliberately
slim subset of ``AnalysisDigest.programs`` shaped for C8
``/architecture-review`` consumption.

All ``Literal`` aliases are paired with an ``_ALL_*`` tuple so
exhaustiveness tests (and downstream consumers — especially the Phase D
auditor and the Phase C renderer prompts) can iterate the allowed values
without re-parsing the type expression. Every literal value MUST appear
verbatim in the Phase C / D system prompts (lands with M5-WRITE /
M5-AUDIT) that ask the LLM to return it, mirroring the M1 / M2 / M3 /
M4 prompt-vs-Literal guard pattern.

The ``schema_version`` field on ``ReconReport``, ``AnalysisDigest``, and
``PlaybookState`` is a closed ``Literal["1.0"]`` so downstream M6
(``/mod-synth-logs``) and consumers can refuse a mismatched output
(design spec §"Output contract" + §"M5 → M6 ingestion contract").

**M-EPIC-3 (#520) abstract entity vocabulary** — issue #522 (M5-MODELS-V2)
introduces six abstract entity types that abstract over per-platform
vocabulary so M5 can ship as a polyglot discovery primitive without
breaking the as-shipped mainframe surface:

* ``ExecutionUnit`` abstracts ``ProgramRecord`` (the thing the runtime
  executes — COBOL program / Python module / Java class / Node route
  handler / …)
* ``SharedComponent`` abstracts ``CopybookRecord`` (code imported by
  ≥2 ExecutionUnits — copybook / shared package / shared library / …)
* ``EntryPoint`` abstracts ``CICSTransaction`` (the thing an outside
  actor invokes — CICS transaction / API route / CLI command / …)
* ``DataStore`` abstracts ``VSAMFileRecord`` (a persistent place data
  lives — VSAM file / DB table / cache / queue / object store)
* ``UIElement`` abstracts ``BMSMapRecord`` (a surface the user sees —
  BMS map / web template / React component / mobile screen)
* ``BatchUnit`` abstracts ``JCLJobRecord`` (a unit of work that runs
  without an interactive caller — JCL job / cron task / Celery beat /
  scheduled workflow)

The six concrete mainframe-vocabulary classes are retained as-is for
backwards compatibility — existing analyser / renderer / auditor code
keeps importing them and constructing them with the mainframe-flavoured
field names (``line_count`` / ``copybook_deps`` / ``cics_transactions``
on ``ProgramRecord``; ``transid`` / ``target_program`` on
``CICSTransaction``; …). The abstract types are the canonical polyglot
surface flavour packs will emit going forward.

``AnalysisDigest`` is *additively* extended in M5-MODELS-V2: the six
existing per-entity fields (``programs`` / ``copybooks`` /
``cics_transactions`` / ``vsam_files`` / ``bms_maps`` / ``jcl_jobs``)
are preserved verbatim alongside four NEW abstract-typed fields
(``execution_units`` / ``shared_components`` / ``entry_points`` /
``data_stores`` / ``ui_elements`` / ``batch_units``). Backwards-compat
``@property`` accessors on the abstract fields filter by ``kind`` so
``digest.cics_transactions_view`` returns the subset of
``digest.entry_points`` with ``kind == "cics_transaction"``. A second
JSON Schema artefact (``skill/references/schemas/analysis_digest_v2.json``) emits
the polyglot-flavoured shape; the v1 schema is preserved for the M-EPIC-2
contract.
"""

# ---------------------------------------------------------------------------
# entities — shared Literals, _ALL_* tuples, and the six abstract BaseModels
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# digest — composite / aggregate models
# ---------------------------------------------------------------------------
from primitives._shared.discovery.models.digest import (
    AnalysisDigest,
    AuditFinding,
    EvidenceRow,
    PlaybookState,
    ReconReport,
)
from primitives._shared.discovery.models.entities import (
    # Exhaustiveness tuples — not in __all__ but importable for consumers
    _ALL_ACCESS_MODES,  # noqa: F401
    _ALL_AUDIT_FINDING_KINDS,  # noqa: F401
    _ALL_BATCH_UNIT_KINDS,  # noqa: F401
    _ALL_CONFIDENCES,  # noqa: F401
    _ALL_DATA_STORE_KINDS,  # noqa: F401
    _ALL_ENTRY_POINT_KINDS,  # noqa: F401
    _ALL_EXECUTION_UNIT_KINDS,  # noqa: F401
    _ALL_MAINFRAME_FLAVORS,  # noqa: F401
    _ALL_PLATFORM_FLAVORS,  # noqa: F401
    _ALL_SEVERITIES,  # noqa: F401
    _ALL_SHARED_COMPONENT_KINDS,  # noqa: F401
    _ALL_SOURCE_LANGUAGES,  # noqa: F401
    _ALL_UI_ELEMENT_KINDS,  # noqa: F401
    AccessMode,
    AuditFindingKind,
    BatchUnit,
    BatchUnitKind,
    Confidence,
    DataStore,
    DataStoreKind,
    DeploymentUnit,
    DeploymentUnitKind,
    EntryPoint,
    EntryPointKind,
    ExecutionUnit,
    ExecutionUnitKind,
    MainframeFlavor,
    PlatformFlavor,
    Severity,
    SharedComponent,
    SharedComponentKind,
    SourceLanguage,
    UIElement,
    UIElementKind,
)

# ---------------------------------------------------------------------------
# legacy — mainframe-specific records
# ---------------------------------------------------------------------------
from primitives._shared.discovery.models.legacy import (
    BMSMapRecord,
    CICSTransaction,
    CopybookRecord,
    JCLJobRecord,
    ProgramInventory,
    ProgramInventoryEntry,
    ProgramRecord,
    VSAMFileRecord,
)

__all__ = [
    "AccessMode",
    "AnalysisDigest",
    "AuditFinding",
    "AuditFindingKind",
    "BMSMapRecord",
    "BatchUnit",
    "BatchUnitKind",
    "CICSTransaction",
    "Confidence",
    "CopybookRecord",
    "DataStore",
    "DataStoreKind",
    "DeploymentUnit",
    "DeploymentUnitKind",
    "EntryPoint",
    "EntryPointKind",
    "EvidenceRow",
    "ExecutionUnit",
    "ExecutionUnitKind",
    "JCLJobRecord",
    "MainframeFlavor",
    "PlatformFlavor",
    "PlaybookState",
    "ProgramInventory",
    "ProgramInventoryEntry",
    "ProgramRecord",
    "ReconReport",
    "Severity",
    "SharedComponent",
    "SharedComponentKind",
    "SourceLanguage",
    "UIElement",
    "UIElementKind",
    "VSAMFileRecord",
]

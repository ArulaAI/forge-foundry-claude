"""M5 /mod-playbook-discovery — public API.

Re-exports the Pydantic model surface authored in M5-MODELS-1 (#443)
plus the Phase A / B entry points needed by cross-primitive consumers.

The Phase A walker (:func:`run_recon`) and the Phase B step-1 aggregator
(:func:`build_program_inventory`) are re-exported here so the M6
``/mod-synth-logs`` primitive (and any future T2 orchestrator) can
consume M5's analysis surface through the public package, not the
internal ``primitives.playbook_discovery.shared.*`` modules. This is the
charter-shaped contract the cross-primitive-shared-imports regression
guard enforces: a primitive may import from another primitive's
top-level package but not from its ``shared/`` internals (see
``tests/test_no_cross_primitive_shared_imports.py``).

The remaining phase modules (``shared/render``, ``shared/audit``) land
behind their own M5-RENDER / M5-AUDIT sub-issues and will surface here
when those phases ship.

See ``primitives/playbook_discovery/SPEC.md`` for the primitive contract
and ``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
§M5 for the canonical design.
"""

from primitives._shared.discovery import (
    CopybookDag,
    CopybookDagEdge,
    ProgramCallEdge,
    build_program_inventory,
)
from primitives._shared.discovery.models import (
    AccessMode,
    AnalysisDigest,
    AuditFinding,
    AuditFindingKind,
    BatchUnit,
    BatchUnitKind,
    BMSMapRecord,
    CICSTransaction,
    Confidence,
    CopybookRecord,
    DataStore,
    DataStoreKind,
    EntryPoint,
    EntryPointKind,
    EvidenceRow,
    ExecutionUnit,
    ExecutionUnitKind,
    JCLJobRecord,
    MainframeFlavor,
    PlatformFlavor,
    PlaybookState,
    ProgramInventory,
    ProgramInventoryEntry,
    ProgramRecord,
    ReconReport,
    Severity,
    SharedComponent,
    SharedComponentKind,
    SourceLanguage,
    UIElement,
    UIElementKind,
    VSAMFileRecord,
)
from primitives._shared.discovery.recon import run_recon

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
    "CopybookDag",
    "CopybookDagEdge",
    "CopybookRecord",
    "DataStore",
    "DataStoreKind",
    "EntryPoint",
    "EntryPointKind",
    "EvidenceRow",
    "ExecutionUnit",
    "ExecutionUnitKind",
    "JCLJobRecord",
    "MainframeFlavor",
    "PlatformFlavor",
    "PlaybookState",
    "ProgramCallEdge",
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
    "build_program_inventory",
    "run_recon",
]

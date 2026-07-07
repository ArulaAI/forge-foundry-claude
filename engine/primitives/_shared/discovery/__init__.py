"""Neutral discovery engine — shared recon + program-inventory + models.

Extracted from the M5 ``playbook_discovery`` primitive so the retained
M6 ``synth_logs`` primitive (and any future T2 orchestrator) can consume
the discovery engine — the Phase A walker (:func:`run_recon`), the Phase
B step-1 aggregator (:func:`build_program_inventory`), the entity models,
and the flavor-pack registry — without depending on M5's skill-first
pipeline (``render`` / ``audit`` / ``sub_call``), which is archived
separately.

This mirrors the existing neutral-shared convention
(``primitives._shared``, ``primitives._llm``, ``primitives._sdet``): a
``_``-prefixed package that is not itself a primitive, so the
cross-primitive-shared-imports regression guard
(``tests/test_no_cross_primitive_shared_imports.py``) treats it as a
shared utility surface rather than another primitive's internals.

The flavor packs register under the
``forge_foundry.playbook_discovery.flavors`` entry-point group, resolved
at import time by :mod:`primitives._shared.discovery.flavor`.
"""

from primitives._shared.discovery.inventory import (
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

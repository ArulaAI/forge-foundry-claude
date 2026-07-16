"""Shared modernization engine (Layer 1) — deterministic, LLM-free machinery.

Cross-primitive package generalized from ``mfx`` (design spec
``docs/superpowers/specs/2026-06-12-mtier-token-efficiency-mfx-engine-design.md``
§3). Imported by the M3 / M4 / M5 ``shared/`` pipelines so the verbose,
deterministic work (digest compression, reference validation, cross-unit
correlation, wave sequencing, Markdown/Mermaid rendering) runs in Python at ~0
model tokens. The model only emits the irreducible compact interpretive
intermediate.

This module exposes Layer-1's data models, the digest compressor (#632),
``refcheck`` (#633), ``correlate`` / ``sequencer`` (#634), and the ``render``
Markdown/Mermaid/traceability helpers (#635).
"""

from __future__ import annotations

from primitives._shared.modernization.correlate import build_system_map
from primitives._shared.modernization.deconstruct import render_deconstruction
from primitives._shared.modernization.digest import compress
from primitives._shared.modernization.hla import (
    HlaDocument,
    HlaSource,
    render_architecture_conformance,
)
from primitives._shared.modernization.models import (
    EngineFacts,
    FactsInterface,
    FactsUnit,
    ModernizationDigest,
    Roadmap,
    RoadmapItem,
    RoadmapWave,
    SystemMap,
    SystemMapEdge,
    SystemMapNode,
)
from primitives._shared.modernization.refcheck import BrokenRef, RefCheckReport
from primitives._shared.modernization.refcheck import check as refcheck
from primitives._shared.modernization.render import (
    TraceRow,
    assign_ids,
    er_diagram,
    flowchart,
    heading,
    md_table,
    sequence_diagram,
    traceability_matrix,
)
from primitives._shared.modernization.schemas import (
    SCHEMA_FILES,
    load_schema,
    validate_artifact,
)
from primitives._shared.modernization.sequencer import order as sequence

__all__ = [
    "SCHEMA_FILES",
    "BrokenRef",
    "EngineFacts",
    "FactsInterface",
    "FactsUnit",
    "HlaDocument",
    "HlaSource",
    "ModernizationDigest",
    "RefCheckReport",
    "Roadmap",
    "RoadmapItem",
    "RoadmapWave",
    "SystemMap",
    "SystemMapEdge",
    "SystemMapNode",
    "TraceRow",
    "assign_ids",
    "build_system_map",
    "compress",
    "er_diagram",
    "flowchart",
    "heading",
    "load_schema",
    "md_table",
    "refcheck",
    "render_architecture_conformance",
    "render_deconstruction",
    "sequence",
    "sequence_diagram",
    "traceability_matrix",
    "validate_artifact",
]

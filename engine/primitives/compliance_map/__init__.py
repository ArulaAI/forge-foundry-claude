"""S70 /compliance-map — public API.

The harness-driven ``compliance_map`` orchestrator and the per-stage LLM entry
points (``identify_touched_controls``, ``find_evidence``) were removed with the
harness layer; the CLI now runs the skill-first extract→judge→continue protocol.
Only the deterministic stages and the shared models are exported.

Usage::

    from primitives.compliance_map import (
        load_catalog,
        select_frameworks,
        analyze_gaps,
        aggregate_ai_quality,
        render_report,
        Framework,
        Control,
        ControlMatch,
        EvidenceRef,
        Gap,
        ComplianceReport,
    )
"""

from primitives.compliance_map.shared.ai_quality_aggregator import (
    AIQualityReport,
    aggregate_ai_quality,
)
from primitives.compliance_map.shared.catalog_loader import ControlCatalog, load_catalog
from primitives.compliance_map.shared.framework_selector import select_frameworks
from primitives.compliance_map.shared.gap_analyzer import analyze_gaps
from primitives.compliance_map.shared.models import (
    ComplianceReport,
    Control,
    ControlMatch,
    EvidenceRef,
    Framework,
    Gap,
)
from primitives.compliance_map.shared.pr_diff_filter import GitDiffError, get_changed_files
from primitives.compliance_map.shared.report_renderer import list_formats, render_report

__all__ = [
    "AIQualityReport",
    "ComplianceReport",
    "Control",
    "ControlCatalog",
    "ControlMatch",
    "EvidenceRef",
    "Framework",
    "Gap",
    "GitDiffError",
    "aggregate_ai_quality",
    "analyze_gaps",
    "get_changed_files",
    "list_formats",
    "load_catalog",
    "render_report",
    "select_frameworks",
]

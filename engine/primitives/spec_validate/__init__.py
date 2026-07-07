"""C5 /spec-validate primitive.

Public API:
    parse_acceptance              — Stage A: reuse from test_from_spec
    build_prompt_items_for_criteria — Stage B+C: per-criterion skill-first prompts
    apply_element_judgments       — Stage B+C: apply in-session judgments
    classify_match                — Stage C: anti-hallucination classify
    read_test_results             — Stage D: pytest JSON reader
    build_prompt_item_for_drift   — Stage E: scope-drift skill-first prompt
    apply_drift_judgment          — Stage E: apply in-session drift judgment
    aggregate_ai_quality          — C10 + C11 sub-calls with dedup
    render_report                 — markdown / JSON / SARIF renderer
    MatchStatus                   — Literal type alias
    Severity                      — Literal type alias
    Confidence                    — Literal type alias
    CodeElementKind               — Literal type alias
    CodeElementRef                — Pydantic model
    AcceptanceTrace               — Pydantic model
    ScopeDriftItem                — Pydantic model
    SpecValidateReport            — Pydantic model
"""

from primitives.spec_validate.shared.acceptance_consumer import (
    AcceptanceCriterion,
    AcceptanceDoc,
    parse_acceptance,
)
from primitives.spec_validate.shared.ai_quality_aggregator import (
    AIQualityReport,
    DedupEntry,
    aggregate_ai_quality,
)
from primitives.spec_validate.shared.code_element_searcher import (
    apply_element_judgments,
    build_prompt_items_for_criteria,
    classify_match,
)
from primitives.spec_validate.shared.models import (
    AcceptanceTrace,
    CodeElementKind,
    CodeElementRef,
    Confidence,
    MatchStatus,
    Severity,
    SpecValidateReport,
)
from primitives.spec_validate.shared.report_renderer import list_formats, render_report
from primitives.spec_validate.shared.scope_drift_detector import (
    apply_drift_judgment,
    build_prompt_item_for_drift,
)
from primitives.spec_validate.shared.test_results_reader import read_test_results

__all__ = [
    "AIQualityReport",
    "AcceptanceCriterion",
    "AcceptanceDoc",
    "AcceptanceTrace",
    "CodeElementKind",
    "CodeElementRef",
    "Confidence",
    "DedupEntry",
    "MatchStatus",
    "ScopeDriftItem",
    "Severity",
    "SpecValidateReport",
    "aggregate_ai_quality",
    "apply_drift_judgment",
    "apply_element_judgments",
    "build_prompt_item_for_drift",
    "build_prompt_items_for_criteria",
    "classify_match",
    "list_formats",
    "parse_acceptance",
    "read_test_results",
    "render_report",
]

# ScopeDriftItem needs explicit re-export
from primitives.spec_validate.shared.models import ScopeDriftItem

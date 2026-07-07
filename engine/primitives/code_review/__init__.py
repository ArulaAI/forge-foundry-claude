"""C4 /code-review primitive.

Public API:
    enumerate_files   — file discovery
    filter_by_confidence — confidence gate
    aggregate_ai_quality — C10 + C11 sub-calls with dedup
    render_report     — markdown / JSON / SARIF renderer
    Lens              — Literal type alias
    Severity          — Literal type alias
    Confidence        — Literal type alias
    CodeReviewIssue   — Pydantic model
    CodeReviewReport  — Pydantic model
"""

from primitives.code_review.shared.ai_quality_aggregator import (
    AIQualityReport,
    DedupEntry,
    aggregate_ai_quality,
)
from primitives.code_review.shared.confidence_filter import filter_by_confidence
from primitives.code_review.shared.file_enumerator import (
    ALWAYS_EXCLUDED_DIRS,
    LANGUAGE_EXTENSIONS,
    enumerate_files,
)
from primitives.code_review.shared.models import (
    CodeReviewIssue,
    CodeReviewReport,
    Confidence,
    Lens,
    Severity,
)
from primitives.code_review.shared.report_renderer import render_report

__all__ = [
    "ALWAYS_EXCLUDED_DIRS",
    "LANGUAGE_EXTENSIONS",
    "AIQualityReport",
    "CodeReviewIssue",
    "CodeReviewReport",
    "Confidence",
    "DedupEntry",
    "Lens",
    "Severity",
    "aggregate_ai_quality",
    "enumerate_files",
    "filter_by_confidence",
    "render_report",
]

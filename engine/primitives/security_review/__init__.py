"""S11 /security-review primitive.

Public API:
    load_security_yaml    -- .foundry/security.yaml loader
    detect_sensitive_areas -- sensitive area detection (Stage A.1)
    filter_by_confidence  -- confidence gate
    annotate_compliance_refs -- S70-aware compliance annotation
    aggregate_ai_quality  -- C10 + C11 sub-calls with dedup
    render_report         -- markdown / JSON / SARIF renderer
    SecurityLens          -- Literal type alias
    OWASPCategory         -- Literal type alias
    Severity              -- Literal type alias
    Confidence            -- Literal type alias
    SecurityFinding       -- Pydantic model
    SensitiveArea         -- Pydantic model
    SecurityReviewReport  -- Pydantic model
"""

from primitives.security_review.shared.ai_quality_aggregator import (
    AIQualityReport,
    DedupEntry,
    aggregate_ai_quality,
)
from primitives.security_review.shared.compliance_annotator import annotate_compliance_refs
from primitives.security_review.shared.confidence_filter import filter_by_confidence
from primitives.security_review.shared.models import (
    Confidence,
    OWASPCategory,
    SecurityFinding,
    SecurityLens,
    SecurityReviewReport,
    SensitiveArea,
    Severity,
)
from primitives.security_review.shared.report_renderer import render_report
from primitives.security_review.shared.security_yaml_loader import (
    SecurityConfig,
    load_security_yaml,
)
from primitives.security_review.shared.sensitive_area import detect_sensitive_areas

__all__ = [
    "AIQualityReport",
    "Confidence",
    "DedupEntry",
    "OWASPCategory",
    "SecurityConfig",
    "SecurityFinding",
    "SecurityLens",
    "SecurityReviewReport",
    "SensitiveArea",
    "Severity",
    "aggregate_ai_quality",
    "annotate_compliance_refs",
    "detect_sensitive_areas",
    "filter_by_confidence",
    "load_security_yaml",
    "render_report",
]

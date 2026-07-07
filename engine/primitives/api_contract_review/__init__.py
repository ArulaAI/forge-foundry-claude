"""S40 /api-contract-review primitive.

Public API:
    detect_contract_format   -- auto-detect contract format from file content
    parse_contract           -- parse contract file -> (Endpoints, format)
    diff_baseline            -- git-ref backward-compat diff
    aggregate_ai_quality     -- C10 + C11 sub-calls with dedup
    render_report            -- markdown / JSON / SARIF renderer
    ContractFormat           -- Literal type alias (5 formats)
    APILens                  -- Literal type alias (7 lenses)
    Severity                 -- Literal type alias
    Confidence               -- Literal type alias
    Endpoint                 -- API endpoint model
    APIFinding               -- Pydantic model
    BaselineDiff             -- Pydantic model
    APIContractReviewReport  -- Pydantic model
"""

from primitives.api_contract_review.shared.ai_quality_aggregator import (
    AIQualityReport,
    DedupEntry,
    aggregate_ai_quality,
)
from primitives.api_contract_review.shared.baseline_differ import diff_baseline
from primitives.api_contract_review.shared.confidence_filter import filter_by_confidence
from primitives.api_contract_review.shared.format_detector import (
    detect_contract_format,
    parse_contract,
)
from primitives.api_contract_review.shared.models import (
    APIContractReviewReport,
    APIFinding,
    APILens,
    BaselineDiff,
    Confidence,
    ContractFormat,
    Endpoint,
    Severity,
)
from primitives.api_contract_review.shared.report_renderer import render_report

__all__ = [
    "AIQualityReport",
    "APIContractReviewReport",
    "APIFinding",
    "APILens",
    "BaselineDiff",
    "Confidence",
    "ContractFormat",
    "DedupEntry",
    "Endpoint",
    "Severity",
    "aggregate_ai_quality",
    "detect_contract_format",
    "diff_baseline",
    "filter_by_confidence",
    "parse_contract",
    "render_report",
]

"""C4 /code-review — Confidence filter.

Maps float confidence_score to Literal Confidence and filters issues
below a given threshold.

Threshold mapping:
  < 0.3  → "low"
  0.3-0.7 → "medium"
  > 0.7  → "high"
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from primitives.code_review.shared.models import CodeReviewIssue, Confidence

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CONFIDENCE_ORDER: dict[Confidence, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
}

_SCORE_HIGH_THRESHOLD: float = 0.7
_SCORE_MEDIUM_THRESHOLD: float = 0.3

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def map_confidence(score: float) -> Confidence:
    """Map a float score to a Confidence Literal.

    Args:
        score: Float in [0.0, 1.0].

    Returns:
        "low" if score < 0.3, "medium" if 0.3 <= score <= 0.7, "high" if > 0.7.
    """
    if score > _SCORE_HIGH_THRESHOLD:
        return "high"
    if score >= _SCORE_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def filter_by_confidence(
    issues: list[CodeReviewIssue],
    threshold: Confidence,
) -> list[CodeReviewIssue]:
    """Return only issues at or above *threshold* confidence.

    Args:
        issues: List of CodeReviewIssue objects.
        threshold: Minimum confidence level ("low", "medium", or "high").

    Returns:
        Filtered list; issues below threshold are dropped.
    """
    min_rank = _CONFIDENCE_ORDER[threshold]
    return [i for i in issues if _CONFIDENCE_ORDER[i.confidence] >= min_rank]

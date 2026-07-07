"""Shared LLM-output parsing utilities (lens-reviewer dedup, #816).

Two pure, stdlib-only helpers that were duplicated verbatim across the three
lens reviewers (``code_review``, ``security_review``, ``api_contract_review``
``shared/lens_reviewer.py``):

- :func:`map_confidence` — bucket a float ``confidence_score`` (0.0-1.0) into the
  ``Literal["low", "medium", "high"]`` confidence band every reviewer uses. The
  ``> 0.7`` / ``>= 0.3`` thresholds match the per-primitive ``_SCORE_HIGH_THRESHOLD``
  / ``_SCORE_MEDIUM_THRESHOLD`` constants byte-for-byte.
- :func:`strip_json_fence` — strip a leading/trailing Markdown code fence
  (``` ```json ``` … ``` ``` ```) off an LLM response before ``json.loads``.

Each primitive imports these functions with aliased names directly::

    from primitives._shared.llm_parsing import map_confidence as _map_confidence
    from primitives._shared.llm_parsing import strip_json_fence as _strip_json_fence

The aliases preserve the local naming convention without wrapping; the return
type of :func:`map_confidence` is the shared ``Confidence`` alias
(``Literal["low", "medium", "high"]``) which all three reviewers use identically.

Imports only stdlib, so it is import-safe under the QUAL-2 (#163) cross-primitive
guard and the QUAL-3 (#164) broad-except guard.
"""

from __future__ import annotations

from typing import Literal

Confidence = Literal["low", "medium", "high"]

_SCORE_HIGH_THRESHOLD: float = 0.7
_SCORE_MEDIUM_THRESHOLD: float = 0.3


def map_confidence(score: float) -> Confidence:
    """Map a float confidence_score (0.0-1.0) to a Literal confidence band."""
    if score > _SCORE_HIGH_THRESHOLD:
        return "high"
    if score >= _SCORE_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def strip_json_fence(text: str) -> str:
    """Strip markdown code fences from LLM output."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Remove first and last lines if they are fence markers
        start = 1
        end = len(lines)
        if lines[-1].strip() == "```":
            end = len(lines) - 1
        stripped = "\n".join(lines[start:end])
    return stripped.strip()

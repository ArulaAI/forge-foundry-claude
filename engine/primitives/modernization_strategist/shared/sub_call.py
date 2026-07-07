"""M3 sub-call (§3.4) — dedup detected antipatterns against C8 findings.

The §3.4 C8 fold is an **optional in-session pass, not a CLI flag** (the Python
CLI never invokes an LLM). When the skill body runs it, it invokes
``/architecture-review`` over the **source code under modernization** — C8's
required ``CODE_PATH`` input, the tree it builds a static dependency graph from
— *not* over ``decomposition.json`` (which is a JSON artifact C8 has no mode to
consume). The resulting C8 findings are then fed here and correlated against the
decomposition's Stage-D antipatterns. :func:`dedup_against_c8` marks each
deterministic :class:`Antipattern` that C8 *also* flagged so the plan does not
double-count a risk both surfaced.

Pure and deterministic — **no LLM, no clock, no subprocess** (the in-session C8
pass lives in the skill body; this module only correlates the two
already-materialized lists). Mirrors the C10/C11 sub-call dedup discipline in
:mod:`primitives.compliance_map.shared.ai_quality_aggregator` (bundle two
finding sources into one frozen result), but the overlap rule is a token
intersection rather than a fixed FM-id join.

Overlap rule
------------

An antipattern overlaps a C8 finding when their *significant tokens* intersect:
the antipattern's ``evidence`` names + the words of its ``detail`` versus the
C8 finding's ``message`` + ``affected`` entity. On overlap the antipattern is
folded in with ``reviewed_by_c8=True`` and a ``c8_note`` carrying C8's wording
(C8 wins — its phrasing is the authoritative one in the plan's risk section).
A C8 finding that matched no antipattern lands in ``c8_only`` (a risk only the
architecture review caught).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from primitives.modernization_strategist.shared.models import (
    Antipattern,  # noqa: TC001 — Pydantic needs it at runtime
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Tokens too generic to carry signal — dropped before intersection so a shared
# stop-word (e.g. "the", "table") can't manufacture a spurious overlap.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "is",
        "are",
        "for",
        "with",
        "by",
        "table",
        "tables",
        "context",
        "contexts",
        "proc",
        "procs",
        "view",
        "views",
        "fk",
        "across",
        "between",
        "multiple",
        "shared",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")


class ReviewedAntipattern(BaseModel):
    """An :class:`Antipattern` annotated with its C8 cross-review status.

    Wraps (rather than mutates) the frozen Stage-D antipattern so the original
    detector output stays immutable. ``reviewed_by_c8`` is ``True`` when a C8
    finding's tokens intersected this antipattern's; ``c8_note`` then carries
    C8's authoritative wording for the plan's risk section.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    antipattern: Antipattern
    reviewed_by_c8: bool = False
    c8_note: str = ""


class SubCallResult(BaseModel):
    """Result of correlating Stage-D antipatterns against C8 findings.

    ``reviewed`` is every antipattern (in input order) wrapped with its C8
    status; ``c8_only`` are the raw C8 findings that matched no antipattern;
    ``overlap_count`` is how many antipatterns C8 also flagged.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reviewed: list[ReviewedAntipattern] = Field(default_factory=list)
    c8_only: list[dict[str, Any]] = Field(default_factory=list)
    overlap_count: int = 0


def _tokens(*parts: str) -> set[str]:
    """Lowercased significant tokens across *parts*, minus stop-words."""
    out: set[str] = set()
    for part in parts:
        for match in _TOKEN_RE.findall(part):
            tok = match.lower()
            if tok and tok not in _STOPWORDS:
                out.add(tok)
    return out


def _antipattern_tokens(ap: Antipattern) -> set[str]:
    return _tokens(ap.detail, *ap.evidence)


def _finding_tokens(finding: Mapping[str, Any]) -> set[str]:
    return _tokens(str(finding.get("message", "")), str(finding.get("affected", "")))


def dedup_against_c8(
    antipatterns: list[Antipattern],
    c8_findings: Sequence[Mapping[str, Any]],
) -> SubCallResult:
    """Correlate Stage-D *antipatterns* against C8 *c8_findings*.

    Args:
        antipatterns: the Stage-D :class:`Antipattern` list (plan.antipatterns).
        c8_findings: generic C8 finding mappings, each ``{"message": str,
            "affected": str, "severity": str}`` (extra keys preserved into
            ``c8_only``).

    Returns:
        A frozen :class:`SubCallResult`: every antipattern wrapped with its C8
        status, the C8-only findings, and the overlap count.
    """
    finding_tokens = [(_finding_tokens(f), dict(f)) for f in c8_findings]
    matched_finding_ids: set[int] = set()

    reviewed: list[ReviewedAntipattern] = []
    overlap = 0
    for ap in antipatterns:
        ap_tokens = _antipattern_tokens(ap)
        note = ""
        hit = False
        for idx, (ftoks, fmap) in enumerate(finding_tokens):
            if ap_tokens & ftoks:
                hit = True
                matched_finding_ids.add(idx)
                # C8 wins — keep its message as the authoritative note.
                if not note:
                    note = str(fmap.get("message", ""))
        if hit:
            overlap += 1
        reviewed.append(ReviewedAntipattern(antipattern=ap, reviewed_by_c8=hit, c8_note=note))

    c8_only = [
        fmap for idx, (_toks, fmap) in enumerate(finding_tokens) if idx not in matched_finding_ids
    ]

    return SubCallResult(reviewed=reviewed, c8_only=c8_only, overlap_count=overlap)


__all__ = [
    "ReviewedAntipattern",
    "SubCallResult",
    "dedup_against_c8",
]

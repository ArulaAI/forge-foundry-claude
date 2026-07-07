"""M2-11 — orchestrator-level anti-hallucination guard.

Validates LLM-classified SP outputs against the deterministic Stage A
parse before they reach the persisted :class:`StoredProcDigest`.

Two guards apply (per SPEC.md §"Anti-hallucination"):

1. **Classification vocabulary** — the
   :data:`primitives.stored_proc_analyze.shared.models.Classification`
   Literal is closed. Stage E (:mod:`bizlogic_classifier`) already
   enforces this on the raw LLM response, but the orchestrator
   re-checks the value as a defence-in-depth measure when assembling
   the final :class:`StoredProc` — that way a future code path that
   bypasses Stage E (e.g. a cache hit, a third-party plugin) cannot
   smuggle an out-of-vocabulary literal into the digest.
2. **``touched_tables`` references** — every name on the final
   :class:`StoredProc.touched_tables` field MUST also appear in the
   AST-extracted set Stage B (:mod:`static_profile`) produced.
   LLM-introduced references (or any other downstream-added table name
   that did not come from the deterministic parse) are dropped with
   a stderr warning.

Both guards never raise — by contract they replace bad data with
"unknown" / a filtered list and surface the original via a
:class:`AntiHallucinationReport` envelope the orchestrator logs.

Mirrors M1's :mod:`primitives.db_schema_analyze.shared.lob_classifier`
closed-vocab guard (PR #367) and M4's ``_apply_anti_hallucination_guard``
pattern (PR #355; M4's Python pipeline was archived in the charter-v2
reshape — ``v1.1.0-pre-archival`` tag). Keeping the guard in a dedicated module rather than
folding it into the orchestrator means the eventual M2 CLI (M2-12),
the runtime orchestrator, and ad-hoc callers all share one
implementation.

Design spec §"Anti-hallucination" + SPEC.md §"Anti-hallucination".
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field

from primitives.stored_proc_analyze.shared.models import (
    _ALL_CLASSIFICATIONS,
    Classification,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed-vocab set
# ---------------------------------------------------------------------------

# Mirrors :data:`_ALL_CLASSIFICATIONS` so the guard fails loudly if the
# Literal grows a member but this module's import is stale. The dedicated
# lockstep test in ``test_anti_hallucination_vocabulary_matches_models``
# locks them together.
_ALLOWED_CLASSIFICATIONS: frozenset[str] = frozenset(_ALL_CLASSIFICATIONS)

_FALLBACK_CLASSIFICATION: Classification = "unknown"
"""The Classification literal substituted when the input value is bad.

``"unknown"`` is the documented out-of-vocab substitute in Stage E and
must match here so downstream consumers see a uniform fallback no matter
which guard fired.
"""


# ---------------------------------------------------------------------------
# Report envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AntiHallucinationReport:
    """Outcome of guarding a single :class:`StoredProc` assembly.

    Frozen so the orchestrator can stash one per SP without worrying
    about mutation. ``classification`` is always one of the eight
    :data:`Classification` literals; ``touched_tables`` is the filtered
    list (every name was present in the AST-extracted set).

    ``warnings`` collects the human-readable notes for both guards (each
    note also goes to stderr + the module logger so a CI operator
    running with ``--no-log-cli`` still sees the drop). When the input
    was clean, ``warnings`` is an empty tuple — round-trip equality of
    the input is preserved exactly.
    """

    sp_name: str
    classification: Classification
    touched_tables: tuple[str, ...]
    dropped_tables: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public guard
# ---------------------------------------------------------------------------


def guard_stored_proc(
    *,
    sp_name: str,
    classification: str,
    candidate_tables: list[str],
    ast_tables: tuple[str, ...] | list[str] | set[str],
) -> AntiHallucinationReport:
    """Validate the LLM-touched fields of a single :class:`StoredProc`.

    Args:
        sp_name: The procedure's name (used only for log messages).
        classification: The Classification value Stage E produced.
            Out-of-vocabulary values are substituted with
            :data:`_FALLBACK_CLASSIFICATION` (``"unknown"``).
        candidate_tables: Table names on the assembled
            :class:`StoredProc.touched_tables` field. Names that are
            not in ``ast_tables`` are dropped with a logged warning.
        ast_tables: The deterministic table set Stage A extracted from
            the AST (typically passed as
            :attr:`primitives.stored_proc_analyze.shared.static_profile.StaticProfile.touched_tables`).

    Returns:
        An :class:`AntiHallucinationReport` carrying the cleaned values,
        the dropped names, and a tuple of warning strings.

    The function never raises — bad input is always coerced to safe
    defaults so the digest write can proceed.
    """
    warnings: list[str] = []

    clean_classification, vocab_warning = _guard_classification(classification, sp_name=sp_name)
    if vocab_warning is not None:
        warnings.append(vocab_warning)

    clean_tables, dropped, table_warning = _guard_touched_tables(
        candidate_tables,
        ast_tables=ast_tables,
        sp_name=sp_name,
    )
    if table_warning is not None:
        warnings.append(table_warning)

    return AntiHallucinationReport(
        sp_name=sp_name,
        classification=clean_classification,
        touched_tables=tuple(clean_tables),
        dropped_tables=tuple(dropped),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Per-field guards
# ---------------------------------------------------------------------------


def _guard_classification(
    raw: str,
    *,
    sp_name: str,
) -> tuple[Classification, str | None]:
    """Coerce ``raw`` to the closed :data:`Classification` vocabulary.

    Returns ``(classification, warning)``. A bad value yields the
    fallback literal and a warning string; a good value returns the
    literal verbatim with ``None`` for the warning. The function never
    raises — defensive coercion is the whole point of the second guard
    pass.
    """
    if raw in _ALLOWED_CLASSIFICATIONS:
        # Narrow the str → Classification Literal for downstream consumers.
        classification: Classification = raw  # type: ignore[assignment]
        return classification, None
    warning = (
        f"orchestrator anti-hallucination: classification {raw!r} for "
        f"sp={sp_name!r} is not in the closed vocabulary; substituting "
        f"{_FALLBACK_CLASSIFICATION!r}"
    )
    _log_guard(warning)
    return _FALLBACK_CLASSIFICATION, warning


def _guard_touched_tables(
    candidates: list[str],
    *,
    ast_tables: tuple[str, ...] | list[str] | set[str],
    sp_name: str,
) -> tuple[list[str], list[str], str | None]:
    """Filter ``candidates`` against the AST-extracted ``ast_tables`` set.

    Returns ``(kept, dropped, warning)``. ``kept`` preserves the
    caller's order so a downstream consumer reading the persisted
    digest sees the same order they would have built without the
    guard. ``dropped`` collects the rejected names for the warning
    message + the :class:`AntiHallucinationReport`.
    """
    allowed = set(ast_tables)
    kept: list[str] = []
    dropped: list[str] = []
    for name in candidates:
        if name in allowed:
            kept.append(name)
        else:
            dropped.append(name)
    if not dropped:
        return kept, dropped, None
    warning = (
        f"orchestrator anti-hallucination: dropped touched_tables entries "
        f"for sp={sp_name!r} that did not appear in the AST-extracted set: "
        f"{dropped}"
    )
    _log_guard(warning)
    return kept, dropped, warning


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_guard(message: str) -> None:
    """Emit the warning on both logging and stderr.

    Logging goes through the module logger so test capture
    (``caplog``) works; the stderr mirror matches the M4
    plan-synthesiser pattern so a CI operator running with
    ``--no-log-cli`` still sees the dropped value.
    """
    logger.warning(message)
    print(f"[stored-proc-analyze] WARNING: {message}", file=sys.stderr)


__all__ = [
    "AntiHallucinationReport",
    "guard_stored_proc",
]

"""M1-10 — orchestrator-level anti-hallucination guard.

Validates LLM-classified Stage D outputs against the deterministic
Stage A / Stage B parse before they reach the persisted
:class:`SchemaDigest`.

Two guards apply (per SPEC.md §"Anti-hallucination"):

1. **LoB classification vocabulary** — the
   :data:`primitives.db_schema_analyze.shared.lob_classifier.LOB_VOCABULARY`
   frozenset is closed. Stage D
   (:mod:`primitives.db_schema_analyze.shared.lob_classifier`) already
   enforces this on the raw LLM response, but the orchestrator
   re-checks the value as a defence-in-depth measure when assembling
   the final :class:`Table` — that way a future code path that bypasses
   Stage D's own guard (e.g. a cache hit, a third-party plugin) cannot
   smuggle an out-of-vocabulary literal into the digest.
2. **Table-reference validation** — every table name that an LLM
   response references (e.g. an ambiguous-table reasoning chain that
   names neighbour tables for context) MUST also appear in the set
   that Stage B (:mod:`object_inventory`) emitted. LLM-introduced
   references are dropped with a stderr + logger warning so the digest
   only carries names that traceback to a deterministic Stage A parse.

Both guards never raise — by contract they replace bad data with
``None`` / a filtered list and surface the original via a logged
warning the orchestrator captures.

Mirrors M2's :mod:`primitives.stored_proc_analyze.shared.anti_hallucination`
(PR #379), M4's ``_apply_anti_hallucination_guard`` pattern (M4's
Python pipeline was archived in the charter-v2 reshape —
``v1.1.0-pre-archival`` tag), and the M1 Stage D
:mod:`primitives.db_schema_analyze.shared.lob_classifier._parse_and_guard`
inline guard. Keeping the guard in a dedicated module rather than
folding it into the orchestrator means the eventual M1 CLI (M1-11), the
runtime orchestrator, and ad-hoc callers all share one implementation.

Design spec §"Anti-hallucination" + SPEC.md §"Anti-hallucination".
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from primitives.db_schema_analyze.shared.lob_classifier import LOB_VOCABULARY

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed-vocab fallback
# ---------------------------------------------------------------------------

_FALLBACK_LOB: None = None
"""The substitute value when the LLM-supplied LoB literal is out-of-vocab.

``None`` matches Stage D's behaviour (an unclassified table carries
``Table.lob_classification = None``) and the SPEC.md "Anti-hallucination"
contract: a rejected LoB literal leaves the table unclassified rather
than carrying an invented label downstream.
"""


# ---------------------------------------------------------------------------
# Report envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AntiHallucinationReport:
    """Outcome of guarding a single :class:`Table`'s LLM-touched fields.

    Frozen so the orchestrator can stash one per table without worrying
    about mutation. ``lob_classification`` is either a literal from
    :data:`LOB_VOCABULARY` or ``None`` (the fallback when the input was
    out-of-vocab). ``table_references`` is the filtered list (every name
    was present in the AST-extracted set).

    ``warnings`` collects the human-readable notes for both guards (each
    note also goes to stderr + the module logger so a CI operator
    running with ``--no-log-cli`` still sees the drop). When the input
    was clean, ``warnings`` is an empty tuple — round-trip equality of
    the input is preserved exactly.
    """

    table_name: str
    lob_classification: str | None
    table_references: tuple[str, ...]
    dropped_references: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public field-level guards (issue #195 spec signatures)
# ---------------------------------------------------------------------------


def validate_table_references(
    llm_output: list[str],
    valid_table_names: set[str],
) -> tuple[list[str], list[str]]:
    """Filter LLM-supplied table names against the AST-extracted set.

    Args:
        llm_output: Table names returned by the LLM (e.g. neighbour
            tables an ambiguous-table reasoning chain cited for
            context). Order is preserved on the returned ``kept`` list
            so digest determinism is not affected.
        valid_table_names: The set of qualified names that Stage B
            extracted from the parsed DDL. Typically populated by the
            orchestrator from
            ``{f"{t.schema_}.{t.name}" if t.schema_ else t.name for t in tables}``.

    Returns:
        ``(kept, dropped)``:

        * ``kept`` — names present in ``valid_table_names``, in caller
          order. The list may be empty.
        * ``dropped`` — names absent from ``valid_table_names``, in
          caller order. Each dropped name is also logged at WARNING
          level (module logger + stderr mirror) so the operator can
          inspect what the LLM tried to inject.

    Never raises — by contract bad input maps to filtering, not an
    exception. A duplicate name in ``llm_output`` is preserved as a
    duplicate on the side it lands on (kept if both copies are valid;
    each copy that is invalid is appended to ``dropped`` once).
    """
    kept: list[str] = []
    dropped: list[str] = []
    for name in llm_output:
        if name in valid_table_names:
            kept.append(name)
        else:
            dropped.append(name)
    if dropped:
        warning = (
            f"anti-hallucination: dropped {len(dropped)} table name(s) not in "
            f"the AST-extracted set (valid set size: {len(valid_table_names)}): "
            f"{dropped}"
        )
        _log_guard(warning)
    return kept, dropped


def validate_lob_classification(
    llm_value: str,
    allowed_lobs: set[str],
) -> str | None:
    """Return ``llm_value`` if it is in ``allowed_lobs``, else ``None``.

    Args:
        llm_value: The LoB literal an LLM response or downstream caller
            supplied (e.g. ``"customer"``, ``"billing"``).
        allowed_lobs: The closed vocabulary that callers permit. The
            typical caller passes :data:`LOB_VOCABULARY` (drawn from the
            YAML pack); ad-hoc callers may pass a narrower set for
            tests.

    Returns:
        ``llm_value`` if in ``allowed_lobs``; ``None`` otherwise (the
        :data:`_FALLBACK_LOB` substitute, matching Stage D's "unclassified"
        contract). When the value is rejected, the original value + the
        sorted allowed set are logged at WARNING level so the operator
        can trace what the LLM tried to inject.

    Never raises — by contract bad input maps to a logged ``None``, not
    an exception. An empty-string input is treated as out-of-vocab; a
    non-string input would be caught by static typing and is not
    defended against at runtime here.
    """
    if llm_value in allowed_lobs:
        return llm_value
    warning = (
        f"anti-hallucination: dropped LoB classification {llm_value!r} (not in "
        f"allowed set of size {len(allowed_lobs)}: {sorted(allowed_lobs)})"
    )
    _log_guard(warning)
    return _FALLBACK_LOB


# ---------------------------------------------------------------------------
# Orchestrator-level table guard
# ---------------------------------------------------------------------------


def guard_table(
    *,
    table_name: str,
    lob_classification: str | None,
    candidate_references: Iterable[str],
    ast_tables: set[str] | Iterable[str],
    allowed_lobs: set[str] | frozenset[str] = LOB_VOCABULARY,
) -> AntiHallucinationReport:
    """Validate the LLM-touched fields of a single :class:`Table`.

    This is the orchestrator entry point — it combines the two
    field-level guards above into a single per-table call so the M1-11
    CLI loop can issue one invocation per table and bundle the warnings
    in a deterministic envelope.

    Args:
        table_name: The qualified table name (used only for log messages).
        lob_classification: The LoB value Stage D produced.
            Out-of-vocabulary values (including ``None``) are passed
            through verbatim — ``None`` is the legitimate Stage D output
            when the regex pack missed and the LLM response was already
            dropped; the orchestrator should not "fix" an honest miss.
            Only a non-``None`` value that fails the closed-vocab check
            is replaced with ``None``.
        candidate_references: Table names downstream callers supplied
            (e.g. an LLM response citing neighbour tables). Names that
            are not in ``ast_tables`` are dropped with a logged warning.
        ast_tables: The deterministic table set Stage B extracted from
            the AST. Callers in a per-table loop should pre-build a
            ``set[str]`` once (e.g.
            ``{f"{t.schema_}.{t.name}" if t.schema_ else t.name for t in tables}``)
            and pass it on every iteration so the conversion is not
            repeated. When an iterable is passed, it is converted to a
            set internally.
        allowed_lobs: The closed LoB vocabulary (default
            :data:`LOB_VOCABULARY`). Exposed so tests can pin a smaller
            set without monkey-patching the YAML pack.

    Returns:
        An :class:`AntiHallucinationReport` carrying the cleaned values,
        the dropped names, and a tuple of warning strings.

    The function never raises — bad input is always coerced to safe
    defaults so the digest write can proceed.
    """
    warnings: list[str] = []

    clean_lob = _guard_lob(lob_classification, allowed_lobs=allowed_lobs, table_name=table_name)
    if clean_lob != lob_classification:
        # Build the message once here so the report carries the same
        # text the logger + stderr already emitted. The dedicated
        # ``_guard_lob`` helper logs at the field-level call site; this
        # surfaces the same context (table name + dropped value) for the
        # report envelope.
        warnings.append(
            f"orchestrator anti-hallucination: lob_classification "
            f"{lob_classification!r} for table={table_name!r} is not in the "
            f"closed vocabulary; substituting {_FALLBACK_LOB!r}"
        )

    # Accept a pre-built set[str] (no-op) or convert an iterable once.
    valid_set: set[str] = ast_tables if isinstance(ast_tables, set) else set(ast_tables)
    candidates_list = list(candidate_references)
    kept, dropped = validate_table_references(candidates_list, valid_set)
    if dropped:
        warnings.append(
            f"orchestrator anti-hallucination: dropped table references for "
            f"table={table_name!r} that did not appear in the AST-extracted "
            f"set: {dropped}"
        )

    return AntiHallucinationReport(
        table_name=table_name,
        lob_classification=clean_lob,
        table_references=tuple(kept),
        dropped_references=tuple(dropped),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Per-field helpers
# ---------------------------------------------------------------------------


def _guard_lob(
    raw: str | None,
    *,
    allowed_lobs: set[str] | frozenset[str],
    table_name: str,
) -> str | None:
    """Coerce ``raw`` to the closed LoB vocabulary or ``None``.

    Returns ``raw`` if it is ``None`` (Stage D's legitimate "no
    classification" output) or if it is in ``allowed_lobs``;
    ``_FALLBACK_LOB`` (``None``) otherwise. A bad value is logged at
    WARNING level on both the module logger and stderr. The function
    never raises — defensive coercion is the whole point of the second
    guard pass.
    """
    if raw is None:
        return None
    if raw in allowed_lobs:
        return raw
    warning = (
        f"orchestrator anti-hallucination: lob_classification {raw!r} for "
        f"table={table_name!r} is not in the closed vocabulary; substituting "
        f"{_FALLBACK_LOB!r}"
    )
    _log_guard(warning)
    return _FALLBACK_LOB


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_guard(message: str) -> None:
    """Emit the warning on both logging and stderr.

    Logging goes through the module logger so test capture
    (``caplog``) works; the stderr mirror matches the M2 / M4
    pattern + the Stage D lob_classifier inline guard so a CI operator
    running with ``--no-log-cli`` still sees the dropped value.
    """
    logger.warning(message)
    print(f"[db-schema-analyze] WARNING: {message}", file=sys.stderr)


__all__ = [
    "AntiHallucinationReport",
    "guard_table",
    "validate_lob_classification",
    "validate_table_references",
]

"""M2 Stage F — ``score_conversion_risk`` (composite per-SP risk score).

Folds the Stage B static profile, Stage C IO shape, Stage D side-effect
surface and the dialect-derived ``cross_db_link`` signal into a single
:class:`primitives.stored_proc_analyze.shared.models.RiskScore`.

The score drives:

* M3 strangler-fig sequencing — higher-risk procedures are sequenced
  later in the extract order so the engineering team accumulates
  modernisation muscle on the easy candidates first.
* M4 conversion prioritisation — the orchestrator picks the lowest
  total scores as the "first wave" of plan-only / spring-batch /
  fastapi conversions.
* The human-readable ``stored-procs-summary.md`` digest, which sorts
  by ``risk_score.total`` descending so reviewers see the conversion
  hazards on page one.

Calibration story is documented in :doc:`docs/m2-risk-calibration` —
the doc is the source of truth for weight justifications, the per-axis
caps, and the deliberately-conservative defaults that intentionally
under-call risk rather than over-call. The C6 deep-review finding on
confidence-gate calibration is the prior art the design borrows from:
"weights without a documented calibration story rot into magic
numbers".

Stage F is **deterministic** — no LLM call, no I/O, no randomness. The
function is pure given ``(profile, io_shape, side_effects, audit_write_count)``. This is
the contract every downstream consumer relies on: re-scoring an
unchanged digest must produce byte-identical output so the orchestrator
can short-circuit on cached digests without re-walking the pipeline.

Design spec §M2 Stage F
(``docs/superpowers/specs/2026-05-22-data-modernization-design.md``).

**#886 — Write-surface / IO-shape / complexity differentiation:**
Prior to this fix the scorer assigned ``total=0`` to every procedure
that lacked a ``dynamic_sql`` side effect, regardless of how many
tables it touched, whether it mixed reads and writes, or how complex its
control flow was. The ``write_breadth`` axis (added to ``RiskScore``)
closes this gap:

* ``table_touch_count`` is bucketed (``// _TABLE_TOUCH_DIVISOR``) and
  capped at ``_PER_AXIS_CAP`` to produce the raw write-breadth score.
* A +1 modifier fires when ``io_shape == "reads_writes"`` — coordinated
  read+write patterns require the migrator to preserve read/write
  ordering semantics that pure write-only procedures do not have.
* The combined value is weighted by ``_WEIGHT_WRITE_BREADTH``.

This ensures a 6-table ETL procedure (e.g. ``sp_calculate_tiered_bills``
with ``table_touch_count=6``, ``io_shape="reads_writes"``) scores
strictly above a trivial single-table CRUD wrapper with the same
cyclomatic complexity and no other risk signals.

**Weight matrix (v3, #886 write_breadth; #1197 6a write_breadth 2→4; #1293 audit_write):**

.. code-block:: text

    total = complexity  * 3   (cyclomatic // 4, capped at 5)
          + side_effects * 2  (non-"none" side-effect surface count, capped at 5)
          + cursor_depth * 4  (5 if cursor_heavy, else 0)
          + dynamic_sql  * 3  (5 if dynamic_sql present, else 0)
          + cross_db     * 4  (5 if cross_db_link, else 0)   [folded into total]
          + write_breadth * 4 (table_touch_count // 2 + 1 if reads_writes, capped at 5)  [#1197 6a]
          + audit_write  * 2  (count of audit/log/notification/outbox writes, cap 5)  [#1293]

    max_parsed = 5*(3+2+4+3+4+2) = 90  (cursor/cross-db mutually exclusive)
    UNPARSEABLE_RISK_SENTINEL = 120   (> 90, human-readable, unambiguous)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.stored_proc_analyze.shared.models import RiskScore

if TYPE_CHECKING:
    from primitives.stored_proc_analyze.shared.models import IOShape, SideEffect
    from primitives.stored_proc_analyze.shared.static_profile import StaticProfile


# ---------------------------------------------------------------------------
# Per-axis caps + weights
# ---------------------------------------------------------------------------
#
# Each component score is an integer in ``[0, _PER_AXIS_CAP]`` so the
# RiskScore Pydantic model can stay int-shaped without losing fidelity
# on the most expensive cases. Capping each axis avoids one signal
# dominating the composite — a 200-LOC SP with cyclomatic 60 should
# not score 5x more risk than a 200-LOC SP with cyclomatic 15 because
# both will still be rewritten by hand; the relative ordering past the
# cap is meaningless for sequencing.
#
# Calibration story: see `docs/m2-risk-calibration.md`. The choices
# below are deliberately conservative — under-calling risk is the
# right default, since an SP that *looks* simple but turns out to be
# hairy will still get human review before M4 conversion. Over-calling
# risk would push otherwise-trivial procedures into the "later wave"
# bucket the M3 sequencer reserves for genuine hazards.

# Cap each component axis at 5 (0 / 1 / 2 / 3 / 4 / 5). Five buckets
# matches the precision the M3 sequencer needs — finer-grained buckets
# would make the composite jitter without making the ordering more
# useful.
_PER_AXIS_CAP: int = 5

# Cyclomatic thresholds: cyclomatic / _CYCLOMATIC_DIVISOR rounded down,
# then clamped to _PER_AXIS_CAP. The 4 here means cyclomatic 1..3 → 0,
# 4..7 → 1, ..., 16..19 → 4, >=20 → 5. McCabe convention treats >10 as
# "complex"; the M2 baseline procs in the fixtures score 1-3, so the
# divisor 4 puts the everyday case at 0 and reserves the cap for
# genuinely hairy bodies.
_CYCLOMATIC_DIVISOR: int = 4

# Side-effects: one point per detected non-``"none"`` surface. With the
# four real surfaces (email, file, external, dynamic_sql) this can
# reach exactly 4 — capped at _PER_AXIS_CAP=5 just for shape uniformity
# (the dynamic_sql axis also contributes once).
_SIDE_EFFECT_PER_SURFACE: int = 1

# Cursor-depth: a binary signal — cursor-heavy SPs get the full cap
# because they typically require a complete redesign (row-by-row →
# set-based or streaming). The intermediate buckets would mislead the
# sequencer; either the SP is cursor-driven or it is not.
_CURSOR_HEAVY_SCORE: int = _PER_AXIS_CAP

# Dynamic-SQL: same binary rationale. Once dynamic SQL appears, the
# conversion plan must include an SQL-injection-safe rewrite, parameter
# binding decisions, and a security review — the cost step-function is
# the same regardless of how many ``EXECUTE IMMEDIATE`` calls fired.
_DYNAMIC_SQL_SCORE: int = _PER_AXIS_CAP

# Cross-DB: the highest signal of all because cross-database links
# typically require an architectural decision the SP team cannot make
# alone (move the link, swap for an API, accept dual-writes). The
# composite weight reflects this — a cross-DB SP scores higher than
# any other single-axis hazard.
_CROSS_DB_SCORE: int = _PER_AXIS_CAP

# Table-touch divisor for the write_breadth axis (#886). Floor-dividing
# table_touch_count by 2 gives: 0-1 tables → 0, 2-3 → 1, 4-5 → 2,
# 6-7 → 3, 8-9 → 4, >=10 → 5 (capped). The divisor 2 is chosen so a
# procedure touching three or more distinct tables earns at least 1 raw
# write-breadth point — this is the minimum cardinality that signals
# "non-trivial data surface" for M3 sequencing purposes.
_TABLE_TOUCH_DIVISOR: int = 2

# Reads-writes modifier: +1 added to the raw table-touch bucket when
# io_shape == "reads_writes". A procedure that both reads and writes
# across tables has tighter read/write ordering constraints than a pure
# write-only procedure — the migrator must preserve read-visibility
# semantics (locking, isolation level, ordering) in addition to the
# write correctness that a writes_only SP already requires. The +1 is
# applied before capping so a 0-table reads_writes still earns 1 raw
# point, and a 10-table reads_writes reaches cap (5) one table earlier.
_READS_WRITES_MODIFIER: int = 1

# Composite weights: per-component multipliers folded into ``composite``
# and ``total``. The weights mirror the design-spec rationale: cursor
# depth, cross-DB, and write_breadth (#1197 6a) are the highest — cursor/
# cross-DB imply architectural rework, and coordinating side-effect writes
# across many tables is a data-integrity / transaction-consistency hazard of
# the same order. Dynamic SQL is high because of the security review burden,
# side-effects are moderate (mapping to a target-framework API is usually
# mechanical but non-trivial), and complexity is the baseline indicator.
_WEIGHT_COMPLEXITY: int = 3
_WEIGHT_SIDE_EFFECTS: int = 2
_WEIGHT_CURSOR_DEPTH: int = 4
_WEIGHT_DYNAMIC_SQL: int = 3
_WEIGHT_CROSS_DB: int = 4
# (#886, recalibrated #1197 6a) Write-surface breadth is a TOP-TIER weight (4,
# matching cursor_depth/cross_db) — coordinating side-effect writes across many
# tables is a data-integrity / transaction-consistency migration hazard on par
# with an architectural rework, not a "moderate" mechanical remap. Raising it
# from 2→4 lets a genuinely multi-table side-effect-write procedure rank at or
# near the top of the risk-descending digest, above a single-axis dynamic-SQL
# procedure, per the M-tier playbook Verify expectation.
_WEIGHT_WRITE_BREADTH: int = 4

# (#1293) Audit-write breadth — the count of audit / log / notification / outbox
# tables the SP writes to. Weight 2 (matching side_effects, below the top-tier
# 4s): a single audit/outbox write is a bounded, well-understood migration task
# (preserve the trail / outbox semantics transactionally), but a proc that fans
# audit + notification writes across several tables is a genuine
# transaction-consistency hazard that must out-rank a same-size pure-domain
# writer. Combined with the +1 this surface already earns in the generic
# side_effects axis, a 2-audit-table reads_writes proc clears a 6-domain-table
# proc — matching the M-tier playbook Verify expectation that the multi-table
# side-effect writer is the single highest-risk proc.
_WEIGHT_AUDIT_WRITE: int = 2

# ---------------------------------------------------------------------------
# Unparseable sentinel (#871)
# ---------------------------------------------------------------------------
#
# An SP that Stage A could not parse has all AST-derived metrics defaulted
# to zero, which would make it score 0 under ``score_conversion_risk`` and
# sort to the BOTTOM of the risk-descending digest — exactly backwards:
# "unknown" is the LEAST understood conversion risk, not the safest.
#
# We assign a sentinel ``total`` that is strictly above any real (parsed)
# composite score.  The real upper bound for a single SP under the current
# weight matrix is:
#
#   max_parsed = _PER_AXIS_CAP * (_WEIGHT_COMPLEXITY + _WEIGHT_SIDE_EFFECTS
#                                  + max(_WEIGHT_CURSOR_DEPTH, _WEIGHT_CROSS_DB)
#                                  + _WEIGHT_DYNAMIC_SQL + _WEIGHT_WRITE_BREADTH
#                                  + _WEIGHT_AUDIT_WRITE)
#              = 5 * (3 + 2 + 4 + 3 + 4 + 2)  =  90   (#1293: + audit_write*2)
#
# (cursor_heavy and cross_db_link are mutually-exclusive IOShape literals so
# only the larger of the two — _WEIGHT_CURSOR_DEPTH == _WEIGHT_CROSS_DB == 4
# — can fire in a single call; the upper bound is therefore exactly 90.)
#
# Setting the sentinel 30 above max_parsed (= 120) leaves a comfortable gap, is
# human-readable in the digest, and cannot be confused with a legitimately
# scored total by any reader who knows the weight matrix.

#: Maximum parsed total under the current weight matrix (cursor/cross-db
#: mutually exclusive, both at cap; all other axes at cap = 90, #1293).  Used
#: to verify that :data:`UNPARSEABLE_RISK_SENTINEL` is strictly greater.
_MAX_PARSED_TOTAL: int = _PER_AXIS_CAP * (
    _WEIGHT_COMPLEXITY
    + _WEIGHT_SIDE_EFFECTS
    + _WEIGHT_CURSOR_DEPTH
    + _WEIGHT_DYNAMIC_SQL
    + _WEIGHT_WRITE_BREADTH
    + _WEIGHT_AUDIT_WRITE
)  # = 5 * (3 + 2 + 4 + 3 + 4 + 2) = 90  (#1293)

#: Sentinel ``total`` assigned to an unparseable SP (#871).
#:
#: Strictly above :data:`_MAX_PARSED_TOTAL` (90) so an unparseable SP
#: always sorts to the **TOP** of the risk-descending digest and sequencing
#: order.  Component axes are set to 0 because the profile is unknown, not
#: because the risk is low — callers must not interpret the 0-axis values as
#: a low-risk verdict.
UNPARSEABLE_RISK_SENTINEL: int = _MAX_PARSED_TOTAL + 30  # = 120 (#1293: was 110)


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def score_conversion_risk_for_unparseable() -> RiskScore:
    """Return a sentinel :class:`RiskScore` for an unparseable stored procedure.

    An unparseable SP has all AST-derived metrics defaulted to zero, which
    would make it score 0 under :func:`score_conversion_risk` and sort to the
    bottom of the risk-descending digest — backwards, because "unknown" is not
    "safe".  This function returns a synthetic :class:`RiskScore` with
    ``total == UNPARSEABLE_RISK_SENTINEL`` (110) so the procedure floats to
    the top of every downstream sort and the human reviewer is forced to look
    at it first.

    Component axes are intentionally left at 0 to signal that the values are
    **unknown** rather than zero.  Only ``composite`` and ``total`` carry the
    sentinel weight.

    The function is **not** a substitute for fixing the grammar gap that caused
    the parse failure; it is a safety net so a parse failure never silently
    masquerades as a trivially-easy conversion candidate.

    Returns:
        A :class:`RiskScore` whose ``total`` equals
        :data:`UNPARSEABLE_RISK_SENTINEL` and whose component axes are 0.
    """
    return RiskScore(
        composite=UNPARSEABLE_RISK_SENTINEL,
        complexity=0,
        side_effects=0,
        cursor_depth=0,
        dynamic_sql=0,
        write_breadth=0,
        audit_write=0,
        total=UNPARSEABLE_RISK_SENTINEL,
    )


def score_conversion_risk(
    *,
    profile: StaticProfile,
    io_shape: IOShape,
    side_effects: list[SideEffect],
    audit_write_count: int = 0,
) -> RiskScore:
    """Return the :class:`RiskScore` for one stored procedure.

    Pure function — same inputs always produce the same RiskScore. The
    orchestrator can therefore cache scores keyed on ``(name, schema)``
    and skip re-scoring on an unchanged digest.

    For procedures with ``parse_status="unparseable"`` the orchestrator
    **must** call :func:`score_conversion_risk_for_unparseable` instead,
    which returns a sentinel score above any real total so the procedure
    sorts to the top of the risk-descending digest rather than the bottom.

    Args:
        profile: Stage B :class:`StaticProfile`. ``cyclomatic`` drives the
            complexity axis; ``table_touch_count`` drives the write_breadth
            axis (#886).
        io_shape: Stage C :data:`IOShape` literal. ``"cursor_heavy"``
            and ``"cross_db_link"`` light their dedicated component
            axes. ``"reads_writes"`` adds a +1 modifier to the raw
            write_breadth score (#886).
        side_effects: Stage D ``list[SideEffect]`` — typically
            includes only the surfaces that fired, with ``"none"``
            standing alone when no surface fired. Both ``"none"`` and
            the real surfaces are tolerated; ``"none"`` does not
            contribute to the score and dynamic_sql is counted via the
            dedicated dynamic-SQL axis (not double-counted in side
            effects).
        audit_write_count: Number of distinct audit / log / notification /
            outbox tables the SP writes to (#1293), from
            ``side_effects.audit_write_tables``. Drives the dedicated
            ``audit_write`` axis (count, capped at ``_PER_AXIS_CAP``). Defaults
            to 0 so callers that do not compute it score ``audit_write=0``.

    Returns:
        A :class:`RiskScore` whose ``composite`` and ``total`` reflect
        the weighted sum of the six component axes. ``total`` is the
        downstream sort key (digest summary, M3 sequencer, M4
        prioritisation).
    """
    complexity = _score_complexity(profile.cyclomatic)
    side_effect_count = _score_side_effects(side_effects)
    cursor_depth = _score_cursor_depth(io_shape)
    dynamic_sql = _score_dynamic_sql(side_effects)
    cross_db = _score_cross_db(io_shape)
    write_breadth = _score_write_breadth(profile.table_touch_count, io_shape)
    audit_write = _score_audit_write(audit_write_count)

    weighted_total = (
        complexity * _WEIGHT_COMPLEXITY
        + side_effect_count * _WEIGHT_SIDE_EFFECTS
        + cursor_depth * _WEIGHT_CURSOR_DEPTH
        + dynamic_sql * _WEIGHT_DYNAMIC_SQL
        + cross_db * _WEIGHT_CROSS_DB
        + write_breadth * _WEIGHT_WRITE_BREADTH
        + audit_write * _WEIGHT_AUDIT_WRITE
    )

    return RiskScore(
        composite=weighted_total,
        complexity=complexity,
        side_effects=side_effect_count,
        cursor_depth=cursor_depth,
        dynamic_sql=dynamic_sql,
        write_breadth=write_breadth,
        audit_write=audit_write,
        total=weighted_total,
    )


# ---------------------------------------------------------------------------
# Per-axis scorers (private)
# ---------------------------------------------------------------------------


def _score_complexity(cyclomatic: int) -> int:
    """Bucket the cyclomatic count into ``[0, _PER_AXIS_CAP]``.

    Floor-division by ``_CYCLOMATIC_DIVISOR`` then clamp. A cyclomatic
    count of 0 (e.g. an unparseable SP whose Stage B profile defaulted
    everything to zero) trivially scores 0 — Stage F does not
    over-penalise a parse failure for the parsed path; the unparseable
    path uses :func:`score_conversion_risk_for_unparseable` instead.
    """
    if cyclomatic <= 0:
        return 0
    bucket = cyclomatic // _CYCLOMATIC_DIVISOR
    return min(bucket, _PER_AXIS_CAP)


def _score_side_effects(side_effects: list[SideEffect]) -> int:
    """Count the non-``"none"`` surfaces present, capped.

    The dynamic-SQL surface contributes via the dedicated dynamic-SQL
    axis, but we intentionally do *not* exclude it from the
    side-effect count: a procedure that does dynamic SQL plus an email
    side-effect is genuinely riskier than one that does dynamic SQL
    alone, and the conservative double-counting flags both axes for
    review.
    """
    surfaces = [s for s in side_effects if s != "none"]
    return min(len(surfaces) * _SIDE_EFFECT_PER_SURFACE, _PER_AXIS_CAP)


def _score_cursor_depth(io_shape: IOShape) -> int:
    """``cursor_heavy`` → cap; any other shape → 0."""
    return _CURSOR_HEAVY_SCORE if io_shape == "cursor_heavy" else 0


def _score_dynamic_sql(side_effects: list[SideEffect]) -> int:
    """``"dynamic_sql"`` present → cap; otherwise → 0."""
    return _DYNAMIC_SQL_SCORE if "dynamic_sql" in side_effects else 0


def _score_cross_db(io_shape: IOShape) -> int:
    """``cross_db_link`` → cap; any other shape → 0."""
    return _CROSS_DB_SCORE if io_shape == "cross_db_link" else 0


def _score_write_breadth(table_touch_count: int, io_shape: IOShape) -> int:
    """Bucket the write-surface breadth into ``[0, _PER_AXIS_CAP]`` (#886).

    Combines two signals:

    1. **Table-touch count** — more distinct tables = larger data-
       dependency footprint = higher migration risk.  Bucketed as
       ``table_touch_count // _TABLE_TOUCH_DIVISOR`` (divisor 2), so:
       0-1 tables → 0, 2-3 → 1, 4-5 → 2, 6-7 → 3, 8-9 → 4, >=10 → 5.

    2. **IO-shape write-surface modifier** — ``reads_writes`` adds 1 to
       the raw bucket before capping.  A procedure that coordinates both
       reads *and* writes across tables carries tighter read-visibility
       constraints (locking, isolation, ordering) than a pure-write
       procedure.  ``writes_only``, ``reads_only``, ``cursor_heavy``, and
       ``cross_db_link`` do not receive the modifier because they are
       already differentiated by their own dedicated axes or because the
       read-only case has no write surface to score.

    Result is clamped to ``_PER_AXIS_CAP`` (5).

    Examples:

    * 1 table, ``writes_only``: ``1 // 2 + 0 = 0`` → raw 0
    * 3 tables, ``reads_writes``: ``3 // 2 + 1 = 2`` → raw 2
    * 6 tables, ``reads_writes``: ``6 // 2 + 1 = 4`` → raw 4
    * 10 tables, ``reads_writes``: ``10 // 2 + 1 = 6`` → capped at 5
    """
    raw = table_touch_count // _TABLE_TOUCH_DIVISOR
    if io_shape == "reads_writes":
        raw += _READS_WRITES_MODIFIER
    return min(raw, _PER_AXIS_CAP)


def _score_audit_write(audit_write_count: int) -> int:
    """Bucket the audit-write table count into ``[0, _PER_AXIS_CAP]`` (#1293).

    A straight count (not floor-divided) — each distinct audit / notification /
    outbox write table is an independent trail/outbox surface to preserve, so
    two such tables score strictly above one. Clamped to the per-axis cap.
    """
    if audit_write_count <= 0:
        return 0
    return min(audit_write_count, _PER_AXIS_CAP)


__all__ = [
    "UNPARSEABLE_RISK_SENTINEL",
    "_CROSS_DB_SCORE",
    "_CURSOR_HEAVY_SCORE",
    "_DYNAMIC_SQL_SCORE",
    "_MAX_PARSED_TOTAL",
    "_PER_AXIS_CAP",
    "_READS_WRITES_MODIFIER",
    "_TABLE_TOUCH_DIVISOR",
    "_WEIGHT_AUDIT_WRITE",
    "_WEIGHT_COMPLEXITY",
    "_WEIGHT_CROSS_DB",
    "_WEIGHT_CURSOR_DEPTH",
    "_WEIGHT_DYNAMIC_SQL",
    "_WEIGHT_SIDE_EFFECTS",
    "_WEIGHT_WRITE_BREADTH",
    "score_conversion_risk",
    "score_conversion_risk_for_unparseable",
]

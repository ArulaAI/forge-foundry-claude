"""M4 Stage E — ``estimate`` (deterministic T-shirt-size effort estimate).

Folds three weighted axes plus a per-target adjustment into an S/M/L/XL
:class:`EffortEstimate`. Pure and deterministic; no LLM.

Calibration source of truth: ``docs/m4-effort-calibration.md``. The constants
below mirror that document; any tuning PR must update both in lockstep and add
a row to the calibration history table (the constants are intentionally
non-public — leading underscore — so the surface stays small).

Composite (per calibration §"Composite")::

    composite = risk_axis  * 3   # RiskScore.total // 8, capped at 5
              + table_axis * 2   # table_touch_count // 2, capped at 5
              + body_axis  * 1   # body_loc // 50, capped at 5
              + target_adjustment

Bucket boundaries (inclusive lower bounds, per calibration §"Bucket boundaries"):

    S  : composite < 5
    M  : 5  <= composite < 12
    L  : 12 <= composite < 22
    XL : composite >= 22

Unparseable SPs (#871):

    When ``resolved.proc.parse_status == "unparseable"`` the M2 digest
    carries a sentinel ``RiskScore.total`` (see
    ``primitives.stored_proc_analyze.shared.risk_scorer.UNPARSEABLE_RISK_SENTINEL``)
    that sits above any legitimately scored total.  The sentinel drives the
    risk-axis to its cap, so the composite skews toward L/XL even when the other
    axes are small.  The :attr:`EffortEstimate.rationale` is additionally
    annotated with ``"estimate unreliable — unparseable source"`` so downstream
    consumers (plan.md, dashboards) cannot mistake the estimate for a reliable
    sizing: the SP body was not parsed, so any size is speculative.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from primitives.sp_converter.shared.models import EffortEstimate

if TYPE_CHECKING:
    from primitives.sp_converter.shared.models import Target
    from primitives.sp_converter.shared.sp_resolver import ResolvedSp

_Size = Literal["S", "M", "L", "XL"]

# --- Per-axis divisors + caps (calibration §"Per-axis rationale") ----------
_RISK_DIVISOR = 8
_TABLE_DIVISOR = 2
_BODY_DIVISOR = 50
_AXIS_CAP = 5

# --- Per-axis weights (calibration §"Composite") ---------------------------
_RISK_WEIGHT = 3
_TABLE_WEIGHT = 2
_BODY_WEIGHT = 1

# --- Bucket boundaries (inclusive lower bounds, calibration §"Bucket boundaries") ---
_M_THRESHOLD = 5
_L_THRESHOLD = 12
_XL_THRESHOLD = 22

# --- Per-target adjustments (calibration §"Per-target adjustments") ---------
_TARGET_ADJUSTMENTS: dict[str, int] = {
    "plan-only": -2,
    "fastapi": 0,
    "spring-boot": 0,
    "spring-batch": 1,
    "celery": 2,
}
#: FLEX-3 third-party renderers fall through to a small positive nudge.
_OTHER_TARGET_ADJUSTMENT = 1

#: Rationale suffix appended when the source SP is unparseable (#871).
#: Consumers that render ``EffortEstimate.rationale`` should surface this
#: text so engineers know the sizing is speculative.
_UNPARSEABLE_RATIONALE_SUFFIX = "estimate unreliable — unparseable source"


def _axis(value: int, divisor: int) -> int:
    """Floor-divide *value* by *divisor*, clamped to ``[0, _AXIS_CAP]``."""
    return min(max(value, 0) // divisor, _AXIS_CAP)


def _bucket(composite: int) -> _Size:
    """Map the integer composite onto its T-shirt bucket."""
    if composite < _M_THRESHOLD:
        return "S"
    if composite < _L_THRESHOLD:
        return "M"
    if composite < _XL_THRESHOLD:
        return "L"
    return "XL"


def estimate(resolved: ResolvedSp, *, target: Target | str) -> EffortEstimate:
    """Return the deterministic :class:`EffortEstimate` for one SP conversion.

    For parseable SPs the estimate is calibrated against the M2 ``RiskScore.total``,
    the resolved table count, and the SP ``body_loc``.

    For **unparseable** SPs (``resolved.proc.parse_status == "unparseable"``) the
    M2 digest carries a sentinel ``RiskScore.total`` that drives the risk axis to
    its cap; the resulting composite skews toward L/XL.  The ``rationale`` field
    is additionally annotated with ``"estimate unreliable — unparseable source"``
    so downstream consumers cannot treat the sizing as a confident number (#871).

    Args:
        resolved: the Stage-A resolution (carries the M2 ``RiskScore.total``,
            the resolved table count, and the SP ``body_loc``).
        target: the conversion target — drives the per-target adjustment.
            Unknown (FLEX-3 plug-in) targets fall through to a ``+1`` nudge.

    Returns:
        An :class:`EffortEstimate` whose ``factors`` dict is the per-axis audit
        trail Stage D's ``plan.md`` renders verbatim.
    """
    proc = resolved.proc
    risk_axis = _axis(proc.risk_score.total, _RISK_DIVISOR)
    table_axis = _axis(len(resolved.tables), _TABLE_DIVISOR)
    body_axis = _axis(proc.body_loc, _BODY_DIVISOR)
    adjustment = _TARGET_ADJUSTMENTS.get(str(target), _OTHER_TARGET_ADJUSTMENT)

    composite = (
        risk_axis * _RISK_WEIGHT
        + table_axis * _TABLE_WEIGHT
        + body_axis * _BODY_WEIGHT
        + adjustment
    )
    size = _bucket(composite)

    factors: dict[str, float] = {
        "risk_total": float(risk_axis),
        "table_touch": float(table_axis),
        "body_loc": float(body_axis),
        "target_adjustment": float(adjustment),
        "weighted_total": float(composite),
    }
    rationale = (
        f"size={size} composite={composite} "
        f"(risk {risk_axis}x{_RISK_WEIGHT} + tables {table_axis}x{_TABLE_WEIGHT} "
        f"+ loc {body_axis}x{_BODY_WEIGHT} + target[{target}] {adjustment:+d})"
    )
    # Unparseable SPs carry a sentinel risk_score.total — the estimate is
    # structurally valid but the sizing cannot be trusted because the body
    # was not parsed (#871).
    if proc.parse_status == "unparseable":
        rationale = f"{rationale} [{_UNPARSEABLE_RATIONALE_SUFFIX}]"

    return EffortEstimate(size=size, score=composite, rationale=rationale, factors=factors)


__all__ = ["estimate"]

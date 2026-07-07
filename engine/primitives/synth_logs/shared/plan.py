"""M6 ``/mod-synth-logs`` Phase B — scenario planning.

Given the closed-universe :class:`EntitiesDigest` Phase A
(:mod:`primitives.synth_logs.shared.ingest`) emits, produce a deterministic
72-hour :class:`ScenarioPlan` for the BankDemo-style 3-day cascading
incident arc.

Determinism contract (design spec §"Phases" Phase B row): the *only*
non-determinism enters via the ``seed`` argument. Same seed against the
same digest produces a byte-identical ``ScenarioPlan``. The implementation
seeds a single :class:`random.Random` instance and drives every choice
(affected-program picks, cascade-target picks, jitter on event timestamps)
through it — no module-global ``random`` calls, no wall-clock reads, no
``datetime.now()``. ``window_start`` is anchored at a fixed UTC epoch
(``ANCHOR_START``) so re-runs share the same timeline; consumers who want
"starting from 3 days ago" can shift the resulting plan downstream.

The 3-day narrative arc (design spec §"Phases" Phase B row +
``/Users/buchar/projects/BankDemo/.claude/commands/synth-logs-generate.md``
Phase 1 §1.1 / §1.4):

* **Day 1** (hours 0..24) — Baseline. No incident events emitted; the
  baseline traffic profile is captured in ``traffic_profile`` and Phase C
  uses it to render normal-mode CICS / VSAM activity.
* **Day 2** (hours 24..48) — Peak. Month-end overlay is on; a handful of
  ``vsam_contention`` events fire intermittently (the foreshadowing the
  reference doc calls *"intermittent issues"*). No cascading abends yet.
* **Day 3** (hours 48..72) — Cascading incident. A single primary
  ``vsam_contention`` event fires inside business hours; within ~5
  minutes it cascades to one or more ``cics_abend`` events on programs
  that read the contended file; a ``batch_overrun`` follows when the
  overnight JCL window slips; one ``network_drop`` correlates with the
  socket-level fallout; the final ``recovery`` event closes the arc 20-30
  minutes after the root cause (per the BankDemo "BUSY condition — 20
  min" annotation in the reference).

The ``baseline`` and ``peak`` variants (M6-PLAN-2 / #468) land alongside
the incident arc and share the same determinism contract, anti-hallucination
layer, and ``traffic_profile`` shape:

* **baseline** — 72 hours of normal-mode banking traffic across every
  day of the window. ``events`` is empty (zero ``IncidentEvent``s);
  ``traffic_profile`` carries the lower-traffic ``baseline`` knobs so
  Phase C renders day-1 quiet activity end-to-end (reference doc
  §"Day 1 (Normal)": 0.5% error rate, occasional NOTFND, no incidents).
* **peak** — 72 hours of month-end / heavy-batch traffic. The
  ``traffic_profile`` carries the higher-traffic ``peak`` knobs (the
  reference doc §"Day 2 (Stressed — Month-End)" pattern stretched across
  the full window). A small set of ``vsam_contention`` events fires
  intermittently across all three days as foreshadowing — but no
  ``cics_abend`` cascade, no ``batch_overrun``, no ``network_drop``, and
  no ``recovery``. Day-3 cascading abend is the ``incident`` arc's
  signature; ``peak`` deliberately stops short of it.

``affected_programs`` and ``cascade_targets`` are drawn deterministically
from ``EntitiesDigest.programs``. The digest folds §5 call-chain targets
back into ``programs`` (see :mod:`primitives.synth_logs.shared.ingest`),
so picking from that set composes real call-chain edges as the
anti-hallucination layer 2 (design spec §"Anti-hallucination") requires.
When the digest carries an empty program list the function still returns
a valid ``ScenarioPlan`` — every event simply lists empty
``affected_programs`` and empty ``cascade_targets``. Phase D's
content-accuracy rule is what fails loudly if downstream renderers leak
references not in the digest.

``scale`` modulates two things: (1) the ``traffic_profile`` numbers Phase
C bakes into the generator script and (2) the number of cascading
``cics_abend`` and intermittent ``vsam_contention`` events scheduled.
The relationship is linear: ``small`` halves the ``mid`` event counts;
``large`` doubles them.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from primitives.synth_logs.shared.models import (
    IncidentEvent,
    IncidentEventKind,
    LogFormat,
    Scenario,
    ScenarioPlan,
)

if TYPE_CHECKING:
    from primitives.synth_logs.shared.models import EntitiesDigest


# ---------------------------------------------------------------------------
# Anchor + window
# ---------------------------------------------------------------------------

ANCHOR_START: Final[datetime] = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
"""Fixed UTC anchor for the 72-hour window. Using a constant guarantees
the determinism contract: same seed against the same digest yields the
same ``window_start`` / ``window_end`` / event ``timestamp`` values.
Consumers that want wall-clock-relative output shift the plan
downstream — the CLI (M6-CLI-1) handles that translation.
"""

MIN_WINDOW_HOURS: Final[int] = 24
"""Minimum window the design spec §"Input flags" allows. ``--window-hours``
below this is rejected by the CLI; the planner mirrors the floor as a
defensive check so direct callers can't slip a sub-24h plan past the SLA
events arithmetic (day boundaries assume 24h granularity)."""


# ---------------------------------------------------------------------------
# Traffic-profile presets (per ``--scale``)
# ---------------------------------------------------------------------------

_TRAFFIC_PROFILES: Final[dict[str, dict[str, float]]] = {
    "small": {
        "off_peak_txn_per_hour": 20.0,
        "peak_txn_per_hour": 200.0,
        "batch_jobs_per_night": 5.0,
        "month_end_overlay_fraction": 0.5,
    },
    "mid": {
        "off_peak_txn_per_hour": 50.0,
        "peak_txn_per_hour": 500.0,
        "batch_jobs_per_night": 10.0,
        "month_end_overlay_fraction": 1.0,
    },
    "large": {
        "off_peak_txn_per_hour": 200.0,
        "peak_txn_per_hour": 5000.0,
        "batch_jobs_per_night": 15.0,
        "month_end_overlay_fraction": 2.0,
    },
}
"""Per-scale traffic-mix knobs Phase C bakes into ``generate_logs.py``.

The numbers match the BankDemo reference (Phase 1 §1.2) and the design
spec §"Input flags" ``--scale`` row. ``month_end_overlay_fraction`` is
a free-form multiplier Phase C uses when sampling the day-2 burst.

The ``incident`` arc uses these numbers verbatim (day 1 baseline traffic
ramps into day 2 peak); the ``baseline`` and ``peak`` arcs (M6-PLAN-2 /
#468) multiply them by a per-scenario factor — see
:data:`_SCENARIO_TRAFFIC_FACTORS`.
"""

# Per-scenario traffic-volume multipliers (M6-PLAN-2 / #468).
#
# ``baseline`` halves the traffic numbers so the 72-hour window renders
# as quiet end-to-end (the reference doc §"Day 1 (Normal)" pattern). The
# month-end overlay drops to zero — quiet days carry no month-end burst.
#
# ``peak`` raises the off-peak / peak / batch knobs (the reference doc
# §"Day 2 (Stressed — Month-End)" pattern stretched across the full
# window) and keeps the month-end overlay above 1.0 so Phase C samples
# the heavy-batch burst on every day, not just day 2.
#
# ``incident`` is the identity factor — the 3-day arc is calibrated
# against the per-scale numbers directly (day 1 = baseline-shaped, day 2
# = peak-shaped, day 3 = cascade).
_SCENARIO_TRAFFIC_FACTORS: Final[dict[Scenario, dict[str, float]]] = {
    "baseline": {
        "off_peak_txn_per_hour": 0.5,
        "peak_txn_per_hour": 0.5,
        "batch_jobs_per_night": 1.0,
        "month_end_overlay_fraction": 0.0,
    },
    "peak": {
        "off_peak_txn_per_hour": 1.5,
        "peak_txn_per_hour": 1.5,
        "batch_jobs_per_night": 1.5,
        "month_end_overlay_fraction": 1.5,
    },
    "incident": {
        "off_peak_txn_per_hour": 1.0,
        "peak_txn_per_hour": 1.0,
        "batch_jobs_per_night": 1.0,
        "month_end_overlay_fraction": 1.0,
    },
}

# Per-scale event counts. Linear: small = mid * 0.5, large = mid * 2.
_DAY2_INTERMITTENT_COUNT: Final[dict[str, int]] = {"small": 1, "mid": 2, "large": 4}
_DAY3_CASCADE_ABENDS: Final[dict[str, int]] = {"small": 1, "mid": 2, "large": 4}

# ``peak`` scenario — intermittent ``vsam_contention`` events fired
# across every day of the window (the reference doc §"Day 2 (Stressed
# — Month-End)" "intermittent issues" pattern stretched to 72h). The
# count is the *total* across the window, distributed evenly across the
# three days so business-hour foreshadowing reads as constant background
# pressure. ``peak`` deliberately stops short of the ``incident`` arc:
# no ``cics_abend`` cascade, no ``batch_overrun``, no ``network_drop``,
# no ``recovery``.
_PEAK_INTERMITTENT_TOTAL: Final[dict[str, int]] = {"small": 3, "mid": 6, "large": 12}


# ---------------------------------------------------------------------------
# Default log-correlation map per event kind
# ---------------------------------------------------------------------------

_EVENT_LOG_CORRELATIONS: Final[dict[IncidentEventKind, tuple[LogFormat, ...]]] = {
    "vsam_contention": (
        "cics_transaction",
        "database",
        "cics_queues",
    ),
    "cics_abend": (
        "cics_transaction",
        "enterprise_server",
        "downstream_calls",
    ),
    "batch_overrun": (
        "enterprise_server",
        "upstream_requests",
    ),
    "network_drop": (
        "network",
        "downstream_calls",
    ),
    "recovery": (
        "enterprise_server",
        "cics_transaction",
        "metrics",
    ),
}
"""For each ``IncidentEventKind``, the closed set of ``LogFormat`` values
in which Phase D expects task IDs for this event to cross-reference. The
choices match the BankDemo reference §"Content requirements" notes for
each log file (e.g. ``vsam_contention`` shows up in cics-transaction +
database + cics-queues; ``cics_abend`` shows up in cics-transaction +
enterprise-server + downstream-calls; etc).
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def plan_scenario(
    entities: EntitiesDigest,
    *,
    scenario: Scenario = "incident",
    window_hours: int = 72,
    scale: str = "mid",
    seed: int = 42,
) -> ScenarioPlan:
    """Compose a deterministic Phase B 72-hour ``ScenarioPlan``.

    Args:
        entities: Phase A's closed-universe catalog. ``programs`` drives
            the ``affected_programs`` / ``cascade_targets`` picks; the
            digest may be empty — events still land, just with empty
            program lists.
        scenario: Narrative arc. ``"incident"`` ships the day-1/2/3
            cascading-abend timeline (M6-PLAN-1). ``"baseline"`` emits a
            quiet 72-hour window with zero ``IncidentEvent``s.
            ``"peak"`` emits a heavy-load 72-hour window with
            intermittent ``vsam_contention`` events spread across all
            three days, but no day-3 cascade (M6-PLAN-2).
        window_hours: Total window duration. Defaults to 72 (the
            design-spec default + the only value the day-1/2/3 incident
            arc is calibrated against). Values below
            :data:`MIN_WINDOW_HOURS` raise ``ValueError``.
        scale: ``"small"`` / ``"mid"`` / ``"large"``. Modulates the
            ``traffic_profile`` numbers + event counts linearly. Unknown
            values raise ``ValueError`` (the CLI uses the same closed
            set; this is a defensive mirror for direct callers).
        seed: Determinism knob. Same seed against the same digest
            produces a byte-identical ``ScenarioPlan``. Defaults to 42
            (a sentinel matching the test-suite convention).

    Returns:
        ``ScenarioPlan`` ready to persist as ``scenario.json``. The plan's
        ``events`` are sorted by timestamp; ``log_specs`` is empty for
        now (Phase B owns the timeline; Phase C composes per-log specs
        from the rendered templates and a follow-up M6 sub-issue will
        populate this field if the design grows to need it).

    Raises:
        ValueError: ``window_hours`` < :data:`MIN_WINDOW_HOURS`,
            ``scale`` is not one of ``"small"`` / ``"mid"`` / ``"large"``,
            or ``scenario`` is not a known :data:`Scenario` literal (the
            CLI uses the same closed set; this is a defensive mirror for
            direct callers).

    Example:
        >>> from primitives.synth_logs.shared.models import EntitiesDigest
        >>> digest = EntitiesDigest(programs=["SBANK00P", "SBANK10P"])
        >>> plan = plan_scenario(digest, seed=42)
        >>> plan.scenario
        'incident'
        >>> plan.seed
        42
        >>> any(ev.event_kind == "recovery" for ev in plan.events)
        True
    """
    if scenario not in _SCENARIO_TRAFFIC_FACTORS:
        valid_scenarios = sorted(_SCENARIO_TRAFFIC_FACTORS)
        msg = f"scenario must be one of {valid_scenarios}; got {scenario!r}."
        raise ValueError(msg)
    if window_hours < MIN_WINDOW_HOURS:
        msg = (
            f"window_hours must be >= {MIN_WINDOW_HOURS}; got {window_hours}. "
            f"The 3-day incident arc assumes 24h day boundaries."
        )
        raise ValueError(msg)
    if scale not in _TRAFFIC_PROFILES:
        valid_scales = sorted(_TRAFFIC_PROFILES)
        msg = f"scale must be one of {valid_scales}; got {scale!r}."
        raise ValueError(msg)

    # ``random.Random`` is the right choice here — the planner emits a
    # *synthetic* incident timeline whose value is determinism + replay,
    # not cryptographic unpredictability. Ruff's S311 fires on every
    # call site so we suppress it locally with a narrowed justification
    # rather than at config scope.
    rng = random.Random(seed)  # noqa: S311 — deterministic test fixture, not crypto

    window_start = ANCHOR_START
    window_end = window_start + timedelta(hours=window_hours)

    traffic_profile = _traffic_profile_for(scenario=scenario, scale=scale)

    events: list[IncidentEvent]
    if scenario == "incident":
        events = _build_incident_events(
            entities=entities,
            scale=scale,
            window_start=window_start,
            rng=rng,
        )
    elif scenario == "baseline":
        events = _build_baseline_events()
    else:  # scenario == "peak"
        events = _build_peak_events(
            entities=entities,
            scale=scale,
            window_start=window_start,
            window_hours=window_hours,
            rng=rng,
        )

    return ScenarioPlan(
        scenario=scenario,
        seed=seed,
        window_start=window_start,
        window_end=window_end,
        traffic_profile=traffic_profile,
        events=events,
        log_specs=[],
        # Pin ``generated_at`` to the window anchor so the determinism
        # contract holds end-to-end. Without this, the model's default
        # factory (``datetime.now(UTC)``) would inject wall-clock drift
        # and break byte-identical reproducibility (issue #467 contract).
        generated_at=window_start,
    )


# ---------------------------------------------------------------------------
# Incident-event composer
# ---------------------------------------------------------------------------


def _build_incident_events(
    *,
    entities: EntitiesDigest,
    scale: str,
    window_start: datetime,
    rng: random.Random,
) -> list[IncidentEvent]:
    """Compose the chronologically-ordered list of day-2/3 incident events.

    Day 1 (hours 0..24) is intentionally empty — the baseline traffic
    profile (in ``traffic_profile``) drives Phase C's normal-mode
    rendering; no scheduled events fire.

    Day 2 (hours 24..48) emits ``_DAY2_INTERMITTENT_COUNT[scale]``
    ``vsam_contention`` events scattered through business hours
    (10..18h on day 2). These foreshadow the day-3 cascade without
    triggering downstream abends.

    Day 3 (hours 48..72) emits the cascade:

    1. one root-cause ``vsam_contention`` event at ~hour 60 (peak business
       traffic on day 3);
    2. ``_DAY3_CASCADE_ABENDS[scale]`` ``cics_abend`` events 30-300s
       after the root cause (the cascading-failure window);
    3. one ``batch_overrun`` ~10 minutes in (the JCL window slips when
       the abends back the queue up);
    4. one ``network_drop`` ~12 minutes in (correlated TCP drops as
       Enterprise Server sheds load);
    5. one ``recovery`` event ~25 minutes after the root cause —
       inside the 20-30 min window the BankDemo reference annotates.
    """
    programs = list(entities.programs)
    events: list[IncidentEvent] = []

    day2_start = window_start + timedelta(hours=24)
    day3_start = window_start + timedelta(hours=48)

    # ---- Day 2: intermittent vsam_contention foreshadowing -----------------
    intermittent_count = _DAY2_INTERMITTENT_COUNT[scale]
    for _ in range(intermittent_count):
        offset_seconds = rng.randint(10 * 3600, 18 * 3600)  # 10am..6pm on day 2
        events.append(
            _make_event(
                event_kind="vsam_contention",
                timestamp=day2_start + timedelta(seconds=offset_seconds),
                programs=programs,
                rng=rng,
                affected_count=1,
                cascade_count=0,
            )
        )

    # ---- Day 3: the cascade ------------------------------------------------
    # Root cause at ~hour 60 of the window (= noon on day 3).
    # rng.randint draws the small jitter that keeps successive runs of
    # different seeds offset from each other while staying inside the
    # business-hour band.
    root_offset = timedelta(hours=12, minutes=rng.randint(0, 30))
    root_ts = day3_start + root_offset
    events.append(
        _make_event(
            event_kind="vsam_contention",
            timestamp=root_ts,
            programs=programs,
            rng=rng,
            affected_count=1,
            cascade_count=_DAY3_CASCADE_ABENDS[scale] + 1,
        )
    )

    # Cascading CICS abends, 30..300s after the root cause.
    cascade_count = _DAY3_CASCADE_ABENDS[scale]
    for _ in range(cascade_count):
        abend_ts = root_ts + timedelta(seconds=rng.randint(30, 300))
        events.append(
            _make_event(
                event_kind="cics_abend",
                timestamp=abend_ts,
                programs=programs,
                rng=rng,
                affected_count=1,
                cascade_count=1,
            )
        )

    # Batch overrun ~10 minutes into the incident.
    events.append(
        _make_event(
            event_kind="batch_overrun",
            timestamp=root_ts + timedelta(minutes=10, seconds=rng.randint(0, 60)),
            programs=programs,
            rng=rng,
            affected_count=1,
            cascade_count=0,
        )
    )

    # Network drop ~12 minutes in (TCP fallout).
    events.append(
        _make_event(
            event_kind="network_drop",
            timestamp=root_ts + timedelta(minutes=12, seconds=rng.randint(0, 60)),
            programs=programs,
            rng=rng,
            affected_count=1,
            cascade_count=0,
        )
    )

    # Recovery at ~hour 25 minutes in (inside the BankDemo "20 min" window).
    events.append(
        _make_event(
            event_kind="recovery",
            timestamp=root_ts + timedelta(minutes=25, seconds=rng.randint(0, 60)),
            programs=programs,
            rng=rng,
            affected_count=1,
            cascade_count=0,
        )
    )

    events.sort(key=lambda ev: ev.timestamp)
    return events


def _build_baseline_events() -> list[IncidentEvent]:
    """Compose the empty event list for the ``baseline`` scenario.

    The baseline arc carries *zero* ``IncidentEvent``s — Phase C uses the
    ``traffic_profile`` numbers alone to render 72 hours of normal-mode
    banking activity (the reference doc §"Day 1 (Normal)" pattern
    stretched across the window). No ``vsam_contention``, no
    ``cics_abend``, no ``batch_overrun``, no ``network_drop``, no
    ``recovery``. The function takes no arguments because nothing
    non-deterministic enters: the event list is unconditionally empty.

    Returns:
        An empty list. The Phase D content-accuracy rule still passes
        because every entity reference is rendered from the closed
        digest at the Phase C layer, not from this list.
    """
    return []


def _build_peak_events(
    *,
    entities: EntitiesDigest,
    scale: str,
    window_start: datetime,
    window_hours: int,
    rng: random.Random,
) -> list[IncidentEvent]:
    """Compose the intermittent-only event list for the ``peak`` scenario.

    The peak arc fires :data:`_PEAK_INTERMITTENT_TOTAL`\\ ``[scale]``
    ``vsam_contention`` events scattered across the entire window inside
    business hours (10..18 local, modelled as offsets from each day's
    midnight). Each event names a single affected program and no
    cascade target — the foreshadowing pattern doesn't propagate.

    The day-3 cascading-abend signature (root-cause vsam_contention →
    cics_abends → batch_overrun → network_drop → recovery) belongs to
    the ``incident`` arc and is deliberately absent here.

    Args:
        entities: closed digest the affected-program picks draw from.
        scale: ``"small"`` / ``"mid"`` / ``"large"`` — sets the event
            count via :data:`_PEAK_INTERMITTENT_TOTAL`.
        window_start: fixed UTC anchor for the 72-hour window.
        window_hours: window length, used to bound which "day" each
            intermittent event lands in. Windows longer than 72 simply
            stretch the per-day distribution; the *total* event count
            is fixed by ``scale``.
        rng: the seeded ``random.Random`` instance driving all picks.

    Returns:
        Chronologically-sorted list of ``vsam_contention`` events.
    """
    programs = list(entities.programs)
    events: list[IncidentEvent] = []

    total = _PEAK_INTERMITTENT_TOTAL[scale]
    # Distribute the total across the window's whole days so a 72h run
    # fires the count evenly across day 1 / 2 / 3. ``window_hours // 24``
    # is the day count (already guarded ≥ 24h above so this is ≥ 1).
    day_count = max(1, window_hours // 24)
    base_per_day, remainder = divmod(total, day_count)
    for day_index in range(day_count):
        # Spread the remainder over the earliest days so smaller counts
        # still land at least one event per active day.
        per_day = base_per_day + (1 if day_index < remainder else 0)
        day_start = window_start + timedelta(hours=24 * day_index)
        for _ in range(per_day):
            # Business-hour band 10am..6pm matches the day-2 intermittent
            # placement in ``_build_incident_events`` so the two arcs
            # share a calibrated "foreshadowing" window.
            offset_seconds = rng.randint(10 * 3600, 18 * 3600)
            events.append(
                _make_event(
                    event_kind="vsam_contention",
                    timestamp=day_start + timedelta(seconds=offset_seconds),
                    programs=programs,
                    rng=rng,
                    affected_count=1,
                    cascade_count=0,
                )
            )

    events.sort(key=lambda ev: ev.timestamp)
    return events


def _traffic_profile_for(*, scenario: Scenario, scale: str) -> dict[str, float]:
    """Apply the per-scenario traffic factors to the per-scale baseline.

    The returned dict has the same keys as :data:`_TRAFFIC_PROFILES`
    (``off_peak_txn_per_hour``, ``peak_txn_per_hour``,
    ``batch_jobs_per_night``, ``month_end_overlay_fraction``); each
    value is the per-scale baseline multiplied by the per-scenario
    factor from :data:`_SCENARIO_TRAFFIC_FACTORS`.

    ``incident`` returns the per-scale baseline verbatim (every factor
    is 1.0). ``baseline`` halves the txn knobs and zeroes the
    month-end overlay. ``peak`` raises every knob by 1.5x.
    """
    base = _TRAFFIC_PROFILES[scale]
    factors = _SCENARIO_TRAFFIC_FACTORS[scenario]
    return {key: base[key] * factors[key] for key in base}


def _make_event(
    *,
    event_kind: IncidentEventKind,
    timestamp: datetime,
    programs: list[str],
    rng: random.Random,
    affected_count: int,
    cascade_count: int,
) -> IncidentEvent:
    """Construct one ``IncidentEvent`` with deterministically-sampled
    program picks.

    When ``programs`` is empty the ``affected_programs`` and
    ``cascade_targets`` lists are returned empty — Phase D's
    content-accuracy rule (not Phase B) is the layer that fails loud on
    a missing closed universe.

    ``programs`` is sampled *with replacement* through manual
    ``rng.choice`` calls so the same program can legitimately appear as
    both an affected program and a cascade target (the BankDemo
    reference's CALL/LINK graph has cycles — ``SBANK10P`` calls
    ``SBANK00P`` which calls ``SBANK10P`` again under recovery).

    ``cascade_targets`` deliberately drops the affected programs so a
    cascade edge always points at a *different* program — the
    cascading-failure narrative wants ``A → B``, not ``A → A``.
    When the program pool is too small to satisfy the cascade-count after
    excluding the affected set, the cascade list is truncated rather
    than padded with placeholders.
    """
    if not programs:
        return IncidentEvent(
            event_kind=event_kind,
            timestamp=timestamp,
            affected_programs=[],
            cascade_targets=[],
            expected_log_correlations=_log_correlations_for(event_kind),
        )
    affected = [rng.choice(programs) for _ in range(affected_count)]
    remainder = [p for p in programs if p not in affected]
    cascade_targets: list[str] = []
    if cascade_count > 0 and remainder:
        # Pick without replacement from the remainder so cascade_targets
        # is a set of distinct downstream programs.
        cascade_targets = rng.sample(
            remainder,
            k=min(cascade_count, len(remainder)),
        )
    return IncidentEvent(
        event_kind=event_kind,
        timestamp=timestamp,
        affected_programs=affected,
        cascade_targets=cascade_targets,
        expected_log_correlations=_log_correlations_for(event_kind),
    )


def _log_correlations_for(event_kind: IncidentEventKind) -> list[LogFormat]:
    """Look up the closed ``LogFormat`` set for an event kind.

    Returns a list (not a tuple) because Pydantic's
    ``IncidentEvent.expected_log_correlations`` is declared as
    ``list[LogFormat]`` — the explicit conversion documents the boundary
    between the immutable static registry (tuples) and the model
    (lists).
    """
    return list(_EVENT_LOG_CORRELATIONS[event_kind])


__all__ = [
    "ANCHOR_START",
    "MIN_WINDOW_HOURS",
    "plan_scenario",
]

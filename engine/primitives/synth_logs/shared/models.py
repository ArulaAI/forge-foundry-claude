"""M6 /mod-synth-logs — Pydantic data models for the four-phase pipeline.

The model surface here matches the M6 design spec §"File contracts" table
and the #463 issue body: Phase A produces an ``EntitiesDigest`` (the
ground-truth catalog parsed from ``PLAYBOOK.md`` or extracted from the
codebase); Phase B produces a ``ScenarioPlan`` containing the 72-hour
deterministic timeline of ``IncidentEvent``s scheduled across day-1/2/3
along with per-log ``LogFileSpec`` metadata; Phase D produces a
``ValidationReport`` plus a top-level ``ManifestRecord`` written to
``logs-manifest.json`` alongside the emitted logs.

Every ``Literal`` alias is paired with an ``_ALL_*`` tuple so
exhaustiveness tests (and downstream consumers — dashboards, the C11
``/production-failure-audit`` sub-call if a consumer wires it manually)
can iterate the allowed values without re-parsing the type expression.
This mirrors the M4-3 (PR #346) pattern.

Every model declares ``model_config = ConfigDict(extra="forbid")`` and
the top-level persisted shapes (``EntitiesDigest``, ``ScenarioPlan``,
``ValidationReport``, ``ManifestRecord``) carry a closed
``schema_version: Literal["1.0"]`` so a consumer can refuse to deserialise
a mismatched output (design spec §"File contracts" rule, restated at the
bottom of the §"File contracts" table).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from primitives._response_schema_validator import accepts_extended_schema

# ---------------------------------------------------------------------------
# Scenario — selects the 72-hour narrative arc Phase B synthesises
# ---------------------------------------------------------------------------

Scenario: TypeAlias = Literal["baseline", "peak", "incident"]
"""``baseline`` = day-1 normal across the window; ``peak`` = month-end /
heavy-batch overlay; ``incident`` = 3-day arc with day-3 cascading abend.
Default ``incident`` per the BankDemo reference (design spec §"Input flags").
"""

_ALL_SCENARIOS: tuple[Scenario, ...] = ("baseline", "peak", "incident")


# ---------------------------------------------------------------------------
# LogFormat — closed enum of the ten log files M6 emits unconditionally
# ---------------------------------------------------------------------------

LogFormat: TypeAlias = Literal[
    "cics_transaction",
    "cics_access",
    "network",
    "database",
    "enterprise_server",
    "downstream_calls",
    "upstream_requests",
    "cics_queues",
    "metrics",
    "security_audit",
]
"""Each value identifies one of the ten log file formats the M6 generator
script emits. Five are text, four are JSONL (``downstream_calls``,
``upstream_requests``, ``security_audit``, plus ``cics_queues`` is text
per spec §"Output contract"), and one is Prometheus snapshot format
(``metrics``). All ten emit unconditionally; absence is a Phase D failure.
"""

_ALL_LOG_FORMATS: tuple[LogFormat, ...] = (
    "cics_transaction",
    "cics_access",
    "network",
    "database",
    "enterprise_server",
    "downstream_calls",
    "upstream_requests",
    "cics_queues",
    "metrics",
    "security_audit",
)


# ---------------------------------------------------------------------------
# IncidentEventKind — closed enum of cascading-incident event kinds
# ---------------------------------------------------------------------------

IncidentEventKind: TypeAlias = Literal[
    "vsam_contention",
    "cics_abend",
    "batch_overrun",
    "network_drop",
    "recovery",
]
"""The five event kinds Phase B uses to compose the day-3 cascading
incident. ``vsam_contention`` is typically the root cause; ``cics_abend``
covers the resulting ASRA/AEY9 cascades; ``batch_overrun`` covers
follow-on JCL timing slips; ``network_drop`` covers correlated TCP /
ESCWA disconnects; ``recovery`` covers the operator-driven restart
sequence (design spec §"Phases" table, Phase B row).
"""

_ALL_INCIDENT_EVENT_KINDS: tuple[IncidentEventKind, ...] = (
    "vsam_contention",
    "cics_abend",
    "batch_overrun",
    "network_drop",
    "recovery",
)


# ---------------------------------------------------------------------------
# ValidationRuleKind — closed enum of Phase D validation rules
# ---------------------------------------------------------------------------

ValidationRuleKind: TypeAlias = Literal[
    "timeline_consistency",
    "ip_port_consistency",
    "transaction_id_correlation",
    "content_accuracy",
]
"""The four rule families Phase D evaluates against the emitted logs.
Each rule contributes one ``ValidationRuleResult`` to the
``ValidationReport`` (design spec §"Phases" table, Phase D row + §M6
"Anti-hallucination").
"""

_ALL_VALIDATION_RULE_KINDS: tuple[ValidationRuleKind, ...] = (
    "timeline_consistency",
    "ip_port_consistency",
    "transaction_id_correlation",
    "content_accuracy",
)


# ---------------------------------------------------------------------------
# EntitiesDigest — Phase A output
# ---------------------------------------------------------------------------


class EntitiesDigest(BaseModel):
    """The ground-truth catalog Phase A extracts from ``PLAYBOOK.md`` (or
    from the codebase directly when no playbook is present).

    Phase A's job is to lock the closed universe Phase B / C / D must
    respect — every CICS transaction emitted into a log, every program
    name, every VSAM file, every copybook, and every IP / hostname / port
    must round-trip back to this digest (design spec §"Anti-hallucination").

    Fields are free-form lists of strings: the M5 ``ProgramInventory``
    Pydantic shape is richer, but for M6's purposes a normalised name
    set is the contract. Phase B / C / D treat any reference to something
    not in this digest as a hard failure (Phase D exit code 4 — design
    spec §"CLI exit codes").
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    transactions: list[str] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    copybooks: list[str] = Field(default_factory=list)
    networks_and_ports: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# IncidentEvent — one scheduled timeline event
# ---------------------------------------------------------------------------


class IncidentEvent(BaseModel):
    """A single scheduled event on the Phase B 72-hour timeline.

    ``event_kind`` is the closed Literal that drives template selection
    in Phase C; ``timestamp`` pins the event onto the deterministic
    timeline (UTC). ``affected_programs`` and ``cascade_targets`` are
    drawn from the Phase A ``EntitiesDigest.programs`` set —
    Phase B's anti-hallucination guard (design spec §"Anti-hallucination"
    layer 2). ``expected_log_correlations`` lists the ``LogFormat`` values
    in which Phase D expects to find this event's task IDs cross-referenced
    (e.g. a ``vsam_contention`` root cause should appear in
    ``cics_transaction``, ``database``, and ``cics_queues``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_kind: IncidentEventKind
    timestamp: datetime
    affected_programs: list[str] = Field(default_factory=list)
    cascade_targets: list[str] = Field(default_factory=list)
    expected_log_correlations: list[LogFormat] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LogFileSpec — per-log-file metadata baked into the generator script
# ---------------------------------------------------------------------------


class LogFileSpec(BaseModel):
    """Per-log-file metadata Phase B emits for every one of the ten
    formats. The Phase C generator (``shared/generate.py`` +
    ``shared/templates/log_*.j2``) reads these specs to bake the line
    count, target path, and time window into ``generate_logs.py``.

    ``entries_count`` is the *floor* per design spec §"Output contract" —
    ``--scale large`` will multiply it. ``time_range_start`` and
    ``time_range_end`` together define the 72-hour (default) window each
    log file must fully cover (Phase D's ``timeline_consistency`` rule).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_name: str
    format: LogFormat
    target_path: str
    entries_count: int = Field(ge=0)
    time_range_start: datetime
    time_range_end: datetime


# ---------------------------------------------------------------------------
# ScenarioPlan — Phase B output
# ---------------------------------------------------------------------------


@accepts_extended_schema
class ScenarioPlan(BaseModel):
    """The deterministic 72-hour plan Phase B emits as ``scenario.json``.

    ``scenario`` records which narrative arc was synthesised (the input
    flag); ``seed`` lets re-runs reproduce the same timeline. The
    ``traffic_profile`` dict is intentionally free-form — it captures the
    per-scale traffic-mix knobs (e.g. ``{"off_peak_txn_per_hour": 50,
    "peak_txn_per_hour": 500, "month_end_overlay_fraction": 0.0}``) that
    feed the per-template renderers without forcing a rigid shape on the
    calibration in `docs/m6-scenario-calibration.md` (lands in a future
    M6 issue).

    ``events`` is the chronologically-ordered list of Phase B
    ``IncidentEvent``s; ``log_specs`` is the per-file metadata Phase C
    consumes. ``window_start`` / ``window_end`` together pin the 72-hour
    window (configurable via ``--window-hours``).

    Gemini subset (issue #548 / SPEC §6.3 + §7.2): persisted M6 Phase-B
    artefact, not an LLM ``response_format`` schema. The free-form
    ``traffic_profile`` dict (``additionalProperties: <schema>``) plus
    the ``$ref`` chains for nested ``IncidentEvent`` / ``LogFileSpec``
    exceed Gemini's accepted subset. Decorated with
    ``@accepts_extended_schema`` because Phase B writes ``scenario.json``
    deterministically to disk for Phase C consumption — Phase B does
    not invoke an LLM with this schema as the structured-output target.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    scenario: Scenario
    seed: int
    window_start: datetime
    window_end: datetime
    traffic_profile: dict[str, float] = Field(default_factory=dict)
    events: list[IncidentEvent] = Field(default_factory=list)
    log_specs: list[LogFileSpec] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---------------------------------------------------------------------------
# ValidationRuleResult — one rule's pass/fail row
# ---------------------------------------------------------------------------


class ValidationRuleResult(BaseModel):
    """Per-rule outcome row inside a ``ValidationReport``.

    ``passed`` is the hard boolean Phase D's exit-code arithmetic reads
    (a single ``False`` here trips exit code 4 or 6 depending on which
    rule fired — design spec §"CLI exit codes"). ``checked`` is the
    sample count Phase D evaluated against (e.g. the 20 random task IDs
    for ``transaction_id_correlation``); ``violations`` is the count of
    failures inside that sample; ``examples`` is a bounded list of
    violation strings used to surface failures in the audit output
    without dumping the full violation set (each example is a short
    human-readable diagnostic — e.g. ``"PROG_FOO referenced in
    cics-transaction.log:482 not in entities.json"``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule: ValidationRuleKind
    passed: bool
    checked: int = Field(ge=0)
    violations: int = Field(ge=0)
    examples: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# ValidationReport — Phase D output
# ---------------------------------------------------------------------------


class ValidationReport(BaseModel):
    """Phase D's aggregate validation report.

    ``rules`` carries one ``ValidationRuleResult`` per
    ``ValidationRuleKind`` literal (Phase D evaluates the closed set);
    ``correlation_rate`` is the per-run cross-file task-ID correlation
    rate (floor 0.95 by default — design spec §"Anti-hallucination").
    ``validation_status`` is the headline boolean every consumer
    dashboard reads.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    rules: list[ValidationRuleResult] = Field(default_factory=list)
    correlation_rate: float = Field(ge=0.0, le=1.0)
    validation_status: bool


# ---------------------------------------------------------------------------
# GeneratedFileRecord — one emitted file's name + sha256 + format
# ---------------------------------------------------------------------------


class GeneratedFileRecord(BaseModel):
    """One emitted log file's path + content sha256 + format.

    The ``ManifestRecord`` lists one of these per ten emitted log files.
    ``sha256`` is the hex-encoded digest of the file's bytes; downstream
    consumers can verify the script's output matches the manifest without
    re-running it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    format: LogFormat


# ---------------------------------------------------------------------------
# ManifestRecord — top-level logs-manifest.json shape
# ---------------------------------------------------------------------------


class ManifestRecord(BaseModel):
    """The top-level record written to ``{--output-dir}/logs-manifest.json``.

    Embeds the ``ScenarioPlan`` metadata the run was driven by, the
    ``ValidationReport`` Phase D produced, the per-file sha256 list
    (``generated_files``), and the day-3 incident correlation summary
    (``expected_day3_incident_correlations``) — the ``IncidentEvent``s
    Phase B scheduled on day 3 that Phase D verified actually correlated
    across the expected log files (design spec §"File contracts", row
    ``M6 → user (audit)``).
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    scenario: Scenario
    seed: int
    generated_files: list[GeneratedFileRecord] = Field(default_factory=list)
    expected_day3_incident_correlations: list[IncidentEvent] = Field(default_factory=list)
    validation: ValidationReport
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


__all__ = [
    "EntitiesDigest",
    "GeneratedFileRecord",
    "IncidentEvent",
    "IncidentEventKind",
    "LogFileSpec",
    "LogFormat",
    "ManifestRecord",
    "Scenario",
    "ScenarioPlan",
    "ValidationReport",
    "ValidationRuleKind",
    "ValidationRuleResult",
]

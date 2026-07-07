"""M6 ``/mod-synth-logs`` Phase D — log validation + manifest emission.

Phase D is the post-generation discipline that catches synthesis errors
*before* a consumer-visible release. The Phase C generator script
(``shared/generate.py``) can drift in three ways the Jinja2 templates'
render-time guards (#470, #471, #472) do not catch:

1. A bad ``ScenarioPlan`` timestamp range that slips a log entry outside
   the declared ``window_start``/``window_end`` window.
2. A bad ``EntitiesDigest`` ingest that omits an entity the templates
   reference (the render-time ``StrictUndefined`` guard catches missing
   *template* variables, not missing *catalog members*).
3. Per-file RNG state that drifts the timeline monotonicity inside a
   single log file (each file is supposed to be chronologically ordered;
   a malformed renderer that shuffles the time index would still pass
   the per-line content guards).

This module owns the four rules the design spec §"Phases" Phase D row
enumerates:

* ``timeline_consistency`` — every timestamp across every log file falls
  inside ``[scenario_plan.window_start, scenario_plan.window_end]`` and
  is monotonically non-decreasing per-file.
* ``ip_port_consistency`` — every ``host:port``-style token in the
  network log resolves to an entry in
  ``entities_digest.networks_and_ports``.
* ``transaction_id_correlation`` — a transaction id that appears in
  multiple log lines must keep the same associated program reference
  (a synth-logs script that lies about correlation is a failure even if
  the entities themselves resolve).
* ``content_accuracy`` — every program / file / transaction-id reference
  in any log file resolves to ``EntitiesDigest`` (anti-hallucination at
  the post-generation stage; complements the render-time guards from
  #470/#471/#472).

The module emits two outputs:

* A :class:`ValidationReport` (model defined in :mod:`models`, PR #484)
  with one :class:`ValidationRuleResult` per rule kind. Each row carries
  ``checked`` (total samples evaluated), ``violations`` (count of
  failures), and ``examples`` (a bounded list of short human-readable
  diagnostics so the audit output never dumps the full violation set).
* A ``logs-manifest.json`` (:class:`ManifestRecord`) written to the
  same directory as the validated logs. The manifest records per-file
  sha256 digests, the input ``ScenarioPlan`` metadata, and the rolled-up
  validation report so a downstream consumer (for example the
  `/production-failure-audit` slash command) can verify the synthesis
  matches the manifest without re-running the generator.

The validator is **graceful on empty input** — an empty log directory
produces a ``ValidationReport`` whose four rules all carry ``checked=0``
and ``passed=True`` and a manifest with an empty ``generated_files``
list. The design spec calls absence of a log file a Phase D *failure*
only when the matching ``LogFileSpec`` declared it; this module enforces
that contract via the same content-accuracy rule path (a missing file
referenced by a spec contributes to the ``content_accuracy`` violation
count rather than raising).

Determinism: the validator iterates file contents in sorted order and
emits manifest records sorted by path. Sha256 digests are stable across
runs by construction. Re-running ``validate_logs`` against the same
inputs returns a ``ValidationReport`` whose JSON-roundtrip is byte-
identical bar the ``generated_at`` timestamp on :class:`ManifestRecord`.

The four rule helpers below are private — the public surface is the
single :func:`validate_logs` function. Tests in
``tests/test_synth_logs_validate.py`` cover each rule's red-green
behaviour (good + bad fixtures) plus the manifest round-trip.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Final

from primitives.synth_logs.shared.models import (
    _ALL_VALIDATION_RULE_KINDS,
    EntitiesDigest,
    GeneratedFileRecord,
    IncidentEvent,
    LogFormat,
    ManifestRecord,
    ScenarioPlan,
    ValidationReport,
    ValidationRuleKind,
    ValidationRuleResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_VIOLATION_EXAMPLES: Final[int] = 5
"""Per-rule cap on sampled violation strings. The full failure count is
preserved on the ``violations`` field; ``examples`` is a bounded slice
so the audit output stays readable when a regression produces thousands
of failures across a large log set."""

CORRELATION_RATE_FLOOR: Final[float] = 0.95
"""Per-design-spec floor (§"Anti-hallucination"). A
``transaction_id_correlation`` rate below this threshold trips Phase D
exit code 6 at the CLI layer. The arithmetic lives here on
:class:`ValidationReport` so the CLI can read it without re-walking the
rule rows."""


# ---------------------------------------------------------------------------
# File-name convention — maps LogFormat literal to the on-disk filename
# the Phase C generator produces (``cics_transaction`` → ``cics-transaction.log``).
# ---------------------------------------------------------------------------

_FORMAT_TO_FILENAME: Final[dict[LogFormat, str]] = {
    "cics_transaction": "cics-transaction.log",
    "cics_access": "cics-access.log",
    "network": "network.log",
    "database": "database.log",
    "enterprise_server": "enterprise-server.log",
    "downstream_calls": "downstream-calls.log",
    "upstream_requests": "upstream-requests.log",
    "cics_queues": "cics-queues.log",
    "metrics": "metrics.log",
    "security_audit": "security-audit.log",
}
"""Inverse mapping is rebuilt at runtime in :func:`_filename_to_format`.
The on-disk filenames mirror the design spec §"Output contract" table."""


_FILENAME_TO_FORMAT: Final[dict[str, LogFormat]] = {v: k for k, v in _FORMAT_TO_FILENAME.items()}


# ---------------------------------------------------------------------------
# Timestamp regex — matches the ISO-8601 UTC stamp the Phase C templates
# emit at the *start* of every log line (e.g.
# ``2024-01-15T08:00:00+00:00 ...``). The pattern is anchored to the line
# start to avoid catching ISO-8601-looking substrings later in the line.
# ---------------------------------------------------------------------------

_TS_PREFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2}))"
)
"""Captures the leading ISO-8601 timestamp on every text-format log
line. JSONL formats (``downstream_calls``, ``upstream_requests``,
``security_audit``) do not match this pattern at the line start; the
timestamp is read out of the parsed JSON object instead.

The regex deliberately tolerates ``Z`` and explicit ``+HH:MM`` offsets
so it survives a future renderer change that prefers compact UTC
suffixes."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_logs(
    log_dir: Path,
    scenario_plan: ScenarioPlan,
    entities_digest: EntitiesDigest,
) -> ValidationReport:
    """Validate every log file in ``log_dir`` and emit ``logs-manifest.json``.

    Walks every ``*.log`` file beneath ``log_dir`` (non-recursive — the
    Phase C generator emits flat into ``--output-dir``), parses
    timestamps and entity references per format, and returns a
    :class:`ValidationReport` whose four rules each contribute one
    :class:`ValidationRuleResult` row.

    Side effect: writes ``log_dir / "logs-manifest.json"`` containing a
    :class:`ManifestRecord` with the per-file sha256 list, the input
    scenario metadata, and the validation report. The manifest is the
    contract a downstream consumer reads to verify the synthesis matches
    what the generator emitted.

    Args:
        log_dir: Directory containing the emitted ``*.log`` files
            (``cics-transaction.log``, ``cics-access.log``, ...).
            The directory may be empty — the report carries
            ``checked=0`` rows in that case.
        scenario_plan: The Phase B plan that drove generation; carries
            ``window_start`` / ``window_end`` for the
            ``timeline_consistency`` rule.
        entities_digest: The Phase A closed catalog every emitted
            reference must resolve to; carries the ``programs`` /
            ``transactions`` / ``files`` / ``networks_and_ports`` sets
            the four rules cross-check against.

    Returns:
        A :class:`ValidationReport` carrying one row per
        :data:`ValidationRuleKind` literal. ``validation_status`` is
        the per-rule AND-fold of every row's ``passed`` field plus the
        ``correlation_rate >= CORRELATION_RATE_FLOOR`` gate.
    """
    log_files = _collect_log_files(log_dir)

    timeline_result = _rule_timeline_consistency(log_files, scenario_plan)
    ip_port_result = _rule_ip_port_consistency(log_files, entities_digest)
    correlation_result = _rule_transaction_id_correlation(log_files)
    content_result = _rule_content_accuracy(log_files, entities_digest)

    rules = [
        timeline_result,
        ip_port_result,
        correlation_result,
        content_result,
    ]

    correlation_rate = _compute_correlation_rate(correlation_result)
    validation_status = (
        all(rule.passed for rule in rules) and correlation_rate >= CORRELATION_RATE_FLOOR
    )

    report = ValidationReport(
        rules=rules,
        correlation_rate=correlation_rate,
        validation_status=validation_status,
    )

    _emit_manifest(log_dir, log_files, scenario_plan, report)

    return report


# ---------------------------------------------------------------------------
# Internals — file collection
# ---------------------------------------------------------------------------


def _collect_log_files(log_dir: Path) -> list[tuple[Path, LogFormat | None]]:
    """Return ``(path, format)`` for every ``*.log`` file in ``log_dir``.

    ``format`` is ``None`` when the filename does not match the closed
    set in ``_FILENAME_TO_FORMAT``; an unknown filename does not raise —
    the content-accuracy rule reports it via an example row instead.

    The list is sorted by path so determinism holds across filesystem
    iteration order differences.
    """
    if not log_dir.exists() or not log_dir.is_dir():
        return []

    files: list[tuple[Path, LogFormat | None]] = []
    for entry in sorted(log_dir.iterdir(), key=lambda p: p.name):
        if not entry.is_file() or entry.suffix != ".log":
            continue
        fmt = _FILENAME_TO_FORMAT.get(entry.name)
        files.append((entry, fmt))
    return files


def _read_text_lines(path: Path) -> list[str]:
    """Read ``path`` as UTF-8 text and return non-empty stripped lines."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [line for line in raw.splitlines() if line.strip()]


def _is_jsonl_format(fmt: LogFormat | None) -> bool:
    """Return ``True`` for the JSONL-shaped log formats.

    Per design spec §"Output contract": ``downstream_calls``,
    ``upstream_requests`` and ``security_audit`` are JSONL. The
    timestamp + entity extraction paths take the JSON branch for these.
    """
    return fmt in {"downstream_calls", "upstream_requests", "security_audit"}


# ---------------------------------------------------------------------------
# Rule 1 — timeline_consistency
# ---------------------------------------------------------------------------


def _rule_timeline_consistency(
    log_files: list[tuple[Path, LogFormat | None]],
    scenario_plan: ScenarioPlan,
) -> ValidationRuleResult:
    """Every timestamp lies inside the scenario window and is per-file
    monotonically non-decreasing.

    Two distinct failure modes counted separately on the same row:

    * **Out-of-window** — a timestamp falls outside
      ``[scenario_plan.window_start, scenario_plan.window_end]``.
    * **Out-of-order** — a timestamp is strictly less than the previous
      timestamp in the same file.

    Both contribute to ``violations``; the bounded ``examples`` list
    mixes both kinds so an audit reader sees representative failures
    of each.
    """
    checked = 0
    violations = 0
    examples: list[str] = []

    window_start = scenario_plan.window_start
    window_end = scenario_plan.window_end

    for path, fmt in log_files:
        previous_ts: datetime | None = None
        timestamps = _extract_timestamps(path, fmt)
        for line_no, ts in timestamps:
            checked += 1
            if ts < window_start or ts > window_end:
                violations += 1
                if len(examples) < MAX_VIOLATION_EXAMPLES:
                    examples.append(
                        f"{path.name}:{line_no} timestamp {ts.isoformat()} "
                        f"outside window [{window_start.isoformat()}, "
                        f"{window_end.isoformat()}]"
                    )
            if previous_ts is not None and ts < previous_ts:
                violations += 1
                if len(examples) < MAX_VIOLATION_EXAMPLES:
                    examples.append(
                        f"{path.name}:{line_no} timestamp {ts.isoformat()} "
                        f"precedes previous {previous_ts.isoformat()}"
                    )
            previous_ts = ts

    return ValidationRuleResult(
        rule="timeline_consistency",
        passed=violations == 0,
        checked=checked,
        violations=violations,
        examples=examples,
    )


def _extract_timestamps(
    path: Path,
    fmt: LogFormat | None,
) -> list[tuple[int, datetime]]:
    """Return ``[(line_no, ts), ...]`` for every parseable timestamp.

    Lines that do not yield a timestamp are silently skipped — the
    ``timeline_consistency`` rule only judges what it can parse. This
    keeps the validator forward-compatible with the stubbed templates
    from #471/#472 that may emit non-timestamped lines until those
    issues land.
    """
    results: list[tuple[int, datetime]] = []
    lines = _read_text_lines(path)
    if _is_jsonl_format(fmt):
        for idx, line in enumerate(lines, start=1):
            ts = _jsonl_timestamp(line)
            if ts is not None:
                results.append((idx, ts))
        return results

    for idx, line in enumerate(lines, start=1):
        match = _TS_PREFIX_RE.match(line)
        if match is None:
            continue
        try:
            ts = datetime.fromisoformat(match.group("ts"))
        except ValueError:
            continue
        results.append((idx, ts))
    return results


def _jsonl_timestamp(line: str) -> datetime | None:
    """Extract an ISO-8601 timestamp from a JSONL log line.

    Looks for the conventional ``timestamp`` key first, then ``ts``,
    then ``time`` — these are the three keys the M6 JSONL templates
    (#471/#472) are expected to use. Returns ``None`` if the line is
    not parseable JSON or no recognised key is present.
    """
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    for key in ("timestamp", "ts", "time"):
        value = obj.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Rule 2 — ip_port_consistency
# ---------------------------------------------------------------------------


# Per ``log_network.j2``: lines look like
#   <ts> <src> -> <dst> <bytes_sent> <bytes_recv> <protocol>
# where ``<src>`` and ``<dst>`` are single tokens drawn from
# ``entities_digest.networks_and_ports``. The regex below extracts those
# two tokens.
_NETWORK_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\S+\s+(?P<src>\S+)\s+->\s+(?P<dst>\S+)\s+")


def _rule_ip_port_consistency(
    log_files: list[tuple[Path, LogFormat | None]],
    entities_digest: EntitiesDigest,
) -> ValidationRuleResult:
    """Every ``src``/``dst`` token in the network log resolves to the
    ``networks_and_ports`` catalog.

    A token that does *not* appear in the catalog is a violation. The
    rule counts each unresolved endpoint reference separately so a line
    with both a bad src AND a bad dst contributes two violations (the
    finer count surfaces partial drift the templates might introduce).
    """
    checked = 0
    violations = 0
    examples: list[str] = []

    catalog = set(entities_digest.networks_and_ports)

    for path, fmt in log_files:
        if fmt != "network":
            continue
        for line_no, line in enumerate(_read_text_lines(path), start=1):
            match = _NETWORK_LINE_RE.match(line)
            if match is None:
                continue
            for kind in ("src", "dst"):
                token = match.group(kind)
                checked += 1
                if token not in catalog:
                    violations += 1
                    if len(examples) < MAX_VIOLATION_EXAMPLES:
                        examples.append(
                            f"{path.name}:{line_no} {kind} {token!r} "
                            "not in entities_digest.networks_and_ports"
                        )

    return ValidationRuleResult(
        rule="ip_port_consistency",
        passed=violations == 0,
        checked=checked,
        violations=violations,
        examples=examples,
    )


# ---------------------------------------------------------------------------
# Rule 3 — transaction_id_correlation
# ---------------------------------------------------------------------------


# Per ``log_cics_transaction.j2``: lines look like
#   <ts> <trans_id> <program> <user_id> <terminal_id> <duration_ms> <outcome>
# where positions 2-5 are the entity references we want to correlate.
_CICS_TRANSACTION_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\S+\s+"
    r"(?P<trans_id>\S+)\s+"
    r"(?P<program>\S+)\s+"
    r"(?P<user_id>\S+)\s+"
    r"(?P<terminal_id>\S+)\s+"
)


def _rule_transaction_id_correlation(
    log_files: list[tuple[Path, LogFormat | None]],
) -> ValidationRuleResult:
    """A transaction id mapped to multiple distinct programs is a lie.

    Walks ``cics-transaction.log`` and builds the ``trans_id → set of
    programs`` index. Any transaction id that maps to more than one
    program is a violation — the synth-logs renderer is asserting
    correlation it does not actually maintain.

    Why this matters: the design spec §"Phases" Phase D row calls out
    cross-file correlation as the lying-vs-truth axis Phase D exists to
    enforce. The current templates do not yet share task IDs across
    files (each file is independently sampled), so the *within-file*
    correlation discipline is what we can verify today. As the
    M6-TEMPLATE-3/4 issues land and start emitting correlated task IDs
    across files, this rule's reach widens naturally — every (trans_id,
    program) tuple from any log file contributes to the same index.

    ``checked`` counts every transaction-line scanned; ``violations``
    counts unique transaction ids that map to >1 program (not the line
    count) so a single lying renderer that emits 10 000 lines does not
    explode the violation count.
    """
    checked = 0
    trans_to_programs: dict[str, set[str]] = {}
    examples: list[str] = []

    for path, fmt in log_files:
        if fmt != "cics_transaction":
            continue
        for line in _read_text_lines(path):
            match = _CICS_TRANSACTION_LINE_RE.match(line)
            if match is None:
                continue
            checked += 1
            trans_id = match.group("trans_id")
            program = match.group("program")
            trans_to_programs.setdefault(trans_id, set()).add(program)

    violations = 0
    for trans_id, programs in sorted(trans_to_programs.items()):
        if len(programs) > 1:
            violations += 1
            if len(examples) < MAX_VIOLATION_EXAMPLES:
                examples.append(
                    f"trans_id {trans_id!r} maps to multiple programs {sorted(programs)!r}"
                )

    return ValidationRuleResult(
        rule="transaction_id_correlation",
        passed=violations == 0,
        checked=checked,
        violations=violations,
        examples=examples,
    )


def _compute_correlation_rate(
    correlation_result: ValidationRuleResult,
) -> float:
    """Convert the rule row into the headline ``correlation_rate``.

    ``checked == 0`` (no cics_transaction lines present) returns 1.0 —
    the empty-input case is not a correlation failure, it is an
    upstream-template-not-yet-implemented case.
    """
    if correlation_result.checked == 0:
        return 1.0
    return 1.0 - (correlation_result.violations / max(correlation_result.checked, 1))


# ---------------------------------------------------------------------------
# Rule 4 — content_accuracy
# ---------------------------------------------------------------------------


# Per ``log_cics_access.j2``: lines look like
#   <ts> <program> <vsam_file> <mode> <record_count> <duration_us>
_CICS_ACCESS_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\S+\s+(?P<program>\S+)\s+(?P<vsam_file>\S+)\s+"
)


# Per ``log_database.j2``: lines look like
#   <ts> <program> <sql_kind> <table> <rows_affected> <duration_ms> <sqlcode>
_DATABASE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\S+\s+(?P<program>\S+)\s+(?P<sql_kind>\S+)\s+(?P<table>\S+)\s+"
)


def _rule_content_accuracy(
    log_files: list[tuple[Path, LogFormat | None]],
    entities_digest: EntitiesDigest,
) -> ValidationRuleResult:
    """Every program / file / transaction reference resolves to the
    digest.

    Per format, the expected entity fields are:

    * ``cics_transaction`` — ``trans_id`` ∈ transactions,
      ``program`` ∈ programs.
    * ``cics_access`` — ``program`` ∈ programs, ``vsam_file`` ∈ files.
    * ``database`` — ``program`` ∈ programs, ``table`` ∈ files.

    Other formats are ignored at this revision — their templates land
    with M6-TEMPLATE-3/4 (#471/#472) and the matching reference-field
    grammars will be wired here when those issues land.

    An unknown filename (``None`` format) contributes one violation per
    file so a renaming drift is surfaced.
    """
    checked = 0
    violations = 0
    examples: list[str] = []

    programs = set(entities_digest.programs)
    files = set(entities_digest.files)
    transactions = set(entities_digest.transactions)

    for path, fmt in log_files:
        if fmt is None:
            violations += 1
            if len(examples) < MAX_VIOLATION_EXAMPLES:
                examples.append(f"{path.name} does not match a known LogFormat filename")
            continue

        if fmt == "cics_transaction":
            for line_no, line in enumerate(_read_text_lines(path), start=1):
                match = _CICS_TRANSACTION_LINE_RE.match(line)
                if match is None:
                    continue
                checked, violations, examples = _check_token(
                    path,
                    line_no,
                    "trans_id",
                    match.group("trans_id"),
                    transactions,
                    "transactions",
                    checked,
                    violations,
                    examples,
                )
                checked, violations, examples = _check_token(
                    path,
                    line_no,
                    "program",
                    match.group("program"),
                    programs,
                    "programs",
                    checked,
                    violations,
                    examples,
                )
        elif fmt == "cics_access":
            for line_no, line in enumerate(_read_text_lines(path), start=1):
                match = _CICS_ACCESS_LINE_RE.match(line)
                if match is None:
                    continue
                checked, violations, examples = _check_token(
                    path,
                    line_no,
                    "program",
                    match.group("program"),
                    programs,
                    "programs",
                    checked,
                    violations,
                    examples,
                )
                checked, violations, examples = _check_token(
                    path,
                    line_no,
                    "vsam_file",
                    match.group("vsam_file"),
                    files,
                    "files",
                    checked,
                    violations,
                    examples,
                )
        elif fmt == "database":
            for line_no, line in enumerate(_read_text_lines(path), start=1):
                match = _DATABASE_LINE_RE.match(line)
                if match is None:
                    continue
                checked, violations, examples = _check_token(
                    path,
                    line_no,
                    "program",
                    match.group("program"),
                    programs,
                    "programs",
                    checked,
                    violations,
                    examples,
                )
                checked, violations, examples = _check_token(
                    path,
                    line_no,
                    "table",
                    match.group("table"),
                    files,
                    "files",
                    checked,
                    violations,
                    examples,
                )
        # network/enterprise_server/downstream_calls/upstream_requests/
        # cics_queues/metrics/security_audit — no token grammar wired yet
        # at this revision. They are checked by the ip_port_consistency
        # rule (network) and will gain content-accuracy grammars as the
        # follow-up template issues (#471/#472) land.

    return ValidationRuleResult(
        rule="content_accuracy",
        passed=violations == 0,
        checked=checked,
        violations=violations,
        examples=examples,
    )


def _check_token(
    path: Path,
    line_no: int,
    field: str,
    token: str,
    catalog: set[str],
    catalog_name: str,
    checked: int,
    violations: int,
    examples: list[str],
) -> tuple[int, int, list[str]]:
    """Single-token catalog check used by :func:`_rule_content_accuracy`.

    Returns the updated ``(checked, violations, examples)`` tuple so the
    caller stays expression-free. ``examples`` is mutated in place when
    capacity remains; a fresh list is never allocated to keep the
    walking-state cheap on large log directories.
    """
    checked += 1
    if token not in catalog:
        violations += 1
        if len(examples) < MAX_VIOLATION_EXAMPLES:
            examples.append(
                f"{path.name}:{line_no} {field} {token!r} not in entities_digest.{catalog_name}"
            )
    return checked, violations, examples


# ---------------------------------------------------------------------------
# Manifest emission
# ---------------------------------------------------------------------------


def _emit_manifest(
    log_dir: Path,
    log_files: list[tuple[Path, LogFormat | None]],
    scenario_plan: ScenarioPlan,
    report: ValidationReport,
) -> None:
    """Write ``log_dir / "logs-manifest.json"`` carrying the run audit.

    ``generated_files`` lists one :class:`GeneratedFileRecord` per known
    log file (filename matches the closed ``LogFormat`` filename set);
    files with an unknown name are *not* recorded in the manifest — the
    content-accuracy rule has already flagged them. The day-3 incident
    correlation summary (``expected_day3_incident_correlations``) pulls
    every :class:`IncidentEvent` from ``scenario_plan.events`` that
    falls inside the day-3 third of the window.
    """
    records: list[GeneratedFileRecord] = []
    for path, fmt in log_files:
        if fmt is None:
            continue
        records.append(
            GeneratedFileRecord(
                path=path.name,
                sha256=_sha256(path),
                format=fmt,
            )
        )

    day3_events = list(_select_day3_events(scenario_plan))

    manifest = ManifestRecord(
        scenario=scenario_plan.scenario,
        seed=scenario_plan.seed,
        generated_files=records,
        expected_day3_incident_correlations=day3_events,
        validation=report,
    )

    manifest_path = log_dir / "logs-manifest.json"
    manifest_path.write_text(
        manifest.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    """Hex sha256 of the file content."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _select_day3_events(plan: ScenarioPlan) -> Iterable[IncidentEvent]:
    """Yield the IncidentEvent rows that fall on day 3 of the window.

    Day 3 is defined as the final third of ``[window_start, window_end]``.
    For the canonical 72-hour window this is the ``window_end - 24h``
    slice. The manifest carries these events verbatim so a consumer can
    cross-check Phase D's correlation discipline against the scheduled
    cascade.
    """
    if not plan.events:
        return iter(())
    span = plan.window_end - plan.window_start
    day3_start = plan.window_end - (span / 3)
    return (event for event in plan.events if event.timestamp >= day3_start)


# ---------------------------------------------------------------------------
# Module-level invariants — fail import if the rule-result list ever drifts.
# ---------------------------------------------------------------------------


def _assert_rule_coverage() -> None:
    """Static guard: every ``ValidationRuleKind`` literal has a rule
    helper above. If a new rule kind is added to ``models.py`` without
    wiring it here, this guard fails at import time.
    """
    wired: set[ValidationRuleKind] = {
        "timeline_consistency",
        "ip_port_consistency",
        "transaction_id_correlation",
        "content_accuracy",
    }
    declared = set(_ALL_VALIDATION_RULE_KINDS)
    missing = declared - wired
    extra = wired - declared
    if missing or extra:
        msg = (
            "ValidationRuleKind drift between models.py and validate.py: "
            f"missing={sorted(missing)!r}, extra={sorted(extra)!r}"
        )
        raise RuntimeError(msg)


_assert_rule_coverage()


__all__ = [
    "CORRELATION_RATE_FLOOR",
    "MAX_VIOLATION_EXAMPLES",
    "validate_logs",
]

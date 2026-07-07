"""C5 /spec-validate — Stage D: test results reader.

Parses pytest JSON output (pytest-json-report format or junitxml) and maps
test names to criteria via naming convention:
  test_<slug>  ->  criterion with id matching slug

When a linked test fails/errors, the trace status is downgraded:
  satisfied  ->  partial

Supports two formats:
- pytest-json-report: JSON with {"tests": [{"nodeid": ..., "outcome": ...}]}
- junitxml JSON: fallback regex extraction from nodeid patterns
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Literal, TypeAlias

from primitives.spec_validate.shared.models import AcceptanceTrace, MatchStatus

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Test outcome types
# ---------------------------------------------------------------------------

TestOutcome: TypeAlias = Literal["passed", "failed", "error", "skipped"]

_PASSING_OUTCOMES: frozenset[str] = frozenset(["passed"])
_FAILING_OUTCOMES: frozenset[str] = frozenset(["failed", "error"])


# ---------------------------------------------------------------------------
# Test result model
# ---------------------------------------------------------------------------


class TestResult:
    """A parsed test result."""

    __slots__ = ("criterion_id", "nodeid", "outcome")

    def __init__(self, nodeid: str, outcome: str, criterion_id: str | None = None) -> None:
        self.nodeid = nodeid
        self.outcome = outcome
        self.criterion_id = criterion_id


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _extract_criterion_id_from_nodeid(nodeid: str) -> str | None:
    """Extract a criterion ID slug from a test node ID.

    Convention: test name must be `test_<slug>` where slug matches a criterion id.

    Example: `tests/test_foo.py::TestBar::test_acceptance_parsing_reuses_c1_helper`
    -> slug = `acceptance_parsing_reuses_c1_helper`

    Args:
        nodeid: pytest node ID (e.g. `tests/test_x.py::ClassName::test_foo`).

    Returns:
        Slug string or None if no match.
    """
    # Extract the test function name (last segment after ::)
    parts = nodeid.split("::")
    _MIN_PARTS = 2
    if len(parts) < _MIN_PARTS:
        return None

    func_name = parts[-1]
    # Match test_<slug>
    m = re.match(r"^test_(.+)$", func_name)
    if not m:
        return None

    slug = m.group(1)
    # Normalize: replace spaces with underscores, lowercase
    slug = slug.lower().replace(" ", "_")
    # Truncate to 40 chars (matching acceptance_parser _slugify behavior)
    return slug[:40]


def parse_pytest_json(report_path: Path) -> list[TestResult]:
    """Parse a pytest-json-report JSON file into TestResult list.

    Supports:
    - {"tests": [{"nodeid": "...", "outcome": "passed|failed|error|skipped"}]}

    Args:
        report_path: Path to the pytest JSON report file.

    Returns:
        List of TestResult instances.

    Raises:
        FileNotFoundError: If report_path does not exist.
        ValueError: If the JSON format is unrecognized.
    """
    if not report_path.exists():
        raise FileNotFoundError(f"Test results not found: {report_path}")

    raw = report_path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in test results: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object, got {type(data).__name__}")

    tests_raw = data.get("tests", [])
    if not isinstance(tests_raw, list):
        raise ValueError("Expected 'tests' key with list value")

    results: list[TestResult] = []
    for test in tests_raw:
        if not isinstance(test, dict):
            continue
        nodeid = test.get("nodeid", "")
        outcome = test.get("outcome", "")
        if not isinstance(nodeid, str) or not isinstance(outcome, str):
            continue
        criterion_id = _extract_criterion_id_from_nodeid(nodeid)
        results.append(TestResult(nodeid=nodeid, outcome=outcome, criterion_id=criterion_id))

    return results


# ---------------------------------------------------------------------------
# Downgrade logic
# ---------------------------------------------------------------------------


def downgrade_traces_on_test_failure(
    traces: list[AcceptanceTrace],
    test_results: list[TestResult],
) -> list[AcceptanceTrace]:
    """Downgrade `satisfied` traces to `partial` when linked tests fail.

    For each trace, check if any test result's criterion_id matches the
    trace's criterion_id. If a matching test failed/errored, downgrade
    `satisfied` -> `partial`.

    Args:
        traces: The acceptance traces from Stage B/C.
        test_results: Parsed test results from Stage D.

    Returns:
        New list of AcceptanceTrace instances (immutable model: creates new instances).
    """
    # Build map: criterion_id -> list of outcomes
    criterion_outcomes: dict[str, list[str]] = {}
    for result in test_results:
        if result.criterion_id:
            if result.criterion_id not in criterion_outcomes:
                criterion_outcomes[result.criterion_id] = []
            criterion_outcomes[result.criterion_id].append(result.outcome)

    updated: list[AcceptanceTrace] = []
    for trace in traces:
        outcomes = criterion_outcomes.get(trace.criterion_id, [])
        has_failure = any(o in _FAILING_OUTCOMES for o in outcomes)

        if has_failure and trace.status == "satisfied":
            # Downgrade: satisfied -> partial
            new_status: MatchStatus = "partial"
            updated.append(
                AcceptanceTrace(
                    criterion_id=trace.criterion_id,
                    criterion_text=trace.criterion_text,
                    status=new_status,
                    code_elements=trace.code_elements,
                    rationale=(
                        f"{trace.rationale} [downgraded: test failure detected for "
                        f"criterion '{trace.criterion_id}']"
                    ),
                    severity="medium",
                    confidence=trace.confidence,
                )
            )
        else:
            updated.append(trace)

    return updated


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def read_test_results(report_path: Path) -> list[TestResult]:
    """Read and parse test results from *report_path*.

    Args:
        report_path: Path to a pytest-json-report JSON file.

    Returns:
        List of TestResult instances.
    """
    return parse_pytest_json(report_path)

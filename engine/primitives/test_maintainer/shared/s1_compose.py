"""S1/S2 composition: invoke S1 sdet_architect scoped-analysis → s1-findings.json.

Composition seam (TM-4, #896):
  S2 test_maintainer calls compose_s1_findings() with the impacted symbol/test set
  (from TM-2 impact.py) and a sampled set of existing test file contents.  The function
  writes the structured s1-findings.json to the run directory and returns the parsed
  findings dict.

  S1 sdet_architect is skill-first (no shared/ Python, no forge-validate CLI command).
  The in-session judgment (convention inference + gap identification) is performed by the
  model via the SKILL.md scoped-analysis flow.  The Python side:
    - Provides load_s1_findings() to read and validate the artifact S1 writes.
    - Provides ScopedAnalysisInput so callers can pass the impacted area uniformly.
    - Documents the composition seam so contributors understand the boundary.

  S1 does NOT re-implement S2's impact engine.
  S2 does NOT re-implement S1's convention inference.

In automated (non-skill-first) test usage, callers can write a synthetic s1-findings.json
directly to the run directory and call load_s1_findings() — the same path S1 would populate
after its scoped-analysis flow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConventionFindings:
    """Testing conventions inferred by S1 from the sampled test files.

    Consumed by the proposer to guide 'update' edits toward house style and
    'add' edits that produce in-style new tests (not generic templates).
    """

    framework: str
    """Primary test framework (e.g. 'pytest', 'jest', 'junit5', 'vitest')."""

    layout: str
    """Directory layout convention (e.g. 'tests/ mirroring src/')."""

    naming: str
    """Function/class naming pattern (e.g. 'test_<thing>_<condition>')."""

    fixture_mock_idioms: str
    """Fixture and mock idioms (e.g. '@pytest.fixture in conftest.py')."""

    assertion_style: str
    """Assertion style (e.g. 'assert <expr>', 'expect(...).toBe(...)')."""

    extra: dict[str, str] = field(default_factory=dict)
    """Optional additional stack-specific observations."""


@dataclass(frozen=True)
class GapEntry:
    """One best-practice gap scoped to the blast radius.

    Consumed by the proposer to guide 'add' proposals that close a real,
    in-style gap rather than proposing generic boilerplate.
    """

    symbol: str
    """The changed/new symbol this gap applies to."""

    gap_type: str
    """Category: missing_negative_test | missing_edge_case | untested_symbol |
    test_smell | missing_contract_test | other."""

    description: str
    """One or two sentence description of the gap and what a closing test should cover."""

    suggested_test_name: str = ""
    """Optional: suggested test function/method name following the repo's naming convention."""


@dataclass(frozen=True)
class S1Findings:
    """Parsed s1-findings.json artifact produced by S1 scoped-analysis mode.

    This is the composition artifact: S1 writes it, S2 reads it.
    """

    schema_version: str
    conventions: ConventionFindings
    gaps: tuple[GapEntry, ...]

    def gaps_for_symbol(self, symbol: str) -> tuple[GapEntry, ...]:
        """Return all gaps whose symbol matches *symbol* exactly."""
        return tuple(g for g in self.gaps if g.symbol == symbol)


# ---------------------------------------------------------------------------
# Load + validate (deterministic — no model call)
# ---------------------------------------------------------------------------


class S1FindingsValidationError(ValueError):
    """Raised when s1-findings.json fails structural validation."""


def load_s1_findings(run_dir: Path) -> S1Findings:
    """Read and parse ``s1-findings.json`` from *run_dir*.

    Raises:
        FileNotFoundError: when the file does not exist.
        S1FindingsValidationError: when the JSON is malformed or fails validation.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    path = _Path(str(run_dir)) / "s1-findings.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"s1-findings.json not found in run_dir={run_dir!r}") from exc

    try:
        data: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise S1FindingsValidationError(f"s1-findings.json is not valid JSON: {exc}") from exc

    return _parse_s1_findings(data)


def _parse_s1_findings(data: object) -> S1Findings:
    """Parse and validate the raw JSON object into an S1Findings dataclass.

    This is the structural validation gate: it mirrors the JSON Schema in
    skill/references/schemas/s1-findings.schema.json so S2 never receives
    a corrupt artifact regardless of where the file came from.
    """
    if not isinstance(data, dict):
        raise S1FindingsValidationError("s1-findings must be a JSON object")

    _require_keys(data, ("schema_version", "conventions", "gaps"), "root")

    if data.get("schema_version") != "1.0":
        raise S1FindingsValidationError(
            f"s1-findings schema_version must be '1.0'; got {data.get('schema_version')!r}"
        )

    conventions = _parse_conventions(data["conventions"])
    gaps = _parse_gaps(data["gaps"])

    return S1Findings(schema_version="1.0", conventions=conventions, gaps=gaps)


def _parse_conventions(raw: object) -> ConventionFindings:
    if not isinstance(raw, dict):
        raise S1FindingsValidationError("s1-findings['conventions'] must be an object")

    _require_str_keys(
        raw,
        ("framework", "layout", "naming", "fixture_mock_idioms", "assertion_style"),
        "conventions",
    )

    extra_raw = raw.get("extra", {})
    if not isinstance(extra_raw, dict):
        raise S1FindingsValidationError("s1-findings['conventions']['extra'] must be an object")
    extra: dict[str, str] = {}
    for k, v in extra_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise S1FindingsValidationError(
                "s1-findings['conventions']['extra'] keys and values must be strings"
            )
        extra[k] = v

    return ConventionFindings(
        framework=str(raw["framework"]),
        layout=str(raw["layout"]),
        naming=str(raw["naming"]),
        fixture_mock_idioms=str(raw["fixture_mock_idioms"]),
        assertion_style=str(raw["assertion_style"]),
        extra=extra,
    )


_VALID_GAP_TYPES: frozenset[str] = frozenset(
    {
        "missing_negative_test",
        "missing_edge_case",
        "untested_symbol",
        "test_smell",
        "missing_contract_test",
        "other",
    }
)


def _parse_gaps(raw: object) -> tuple[GapEntry, ...]:
    if not isinstance(raw, list):
        raise S1FindingsValidationError("s1-findings['gaps'] must be an array")

    gaps: list[GapEntry] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise S1FindingsValidationError(f"s1-findings['gaps'][{i}] must be an object")
        _require_str_keys(item, ("symbol", "gap_type", "description"), f"gaps[{i}]")
        gt = str(item["gap_type"])
        if gt not in _VALID_GAP_TYPES:
            raise S1FindingsValidationError(
                f"s1-findings['gaps'][{i}]['gap_type'] {gt!r} not in allowed set: "
                + ", ".join(sorted(_VALID_GAP_TYPES))
            )
        suggested = item.get("suggested_test_name", "")
        if not isinstance(suggested, str):
            raise S1FindingsValidationError(
                f"s1-findings['gaps'][{i}]['suggested_test_name'] must be a string"
            )
        gaps.append(
            GapEntry(
                symbol=str(item["symbol"]),
                gap_type=gt,
                description=str(item["description"]),
                suggested_test_name=suggested,
            )
        )
    return tuple(gaps)


def _require_keys(obj: dict[str, object], keys: tuple[str, ...], ctx: str) -> None:
    missing = [k for k in keys if k not in obj]
    if missing:
        raise S1FindingsValidationError(f"s1-findings[{ctx!r}] missing required keys: {missing}")


def _require_str_keys(obj: dict[str, object], keys: tuple[str, ...], ctx: str) -> None:
    _require_keys(obj, keys, ctx)
    for k in keys:
        if not isinstance(obj[k], str):
            raise S1FindingsValidationError(
                f"s1-findings[{ctx!r}][{k!r}] must be a string; got {type(obj[k]).__name__}"
            )

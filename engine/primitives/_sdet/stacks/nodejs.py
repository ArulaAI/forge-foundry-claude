"""JS/TS test stack detection + execution adapter.

The detection half (``detect_nodejs_stack`` → :class:`StackProfile`)
reads package.json deps + scripts to determine: language (js/ts),
test framework (jest|vitest|mocha|playwright|none), and e2e framework
(playwright when present alongside another).

The execution half (:class:`NodeStackAdapter`, S2 TM-8 #900) implements
the :class:`~primitives._sdet.stacks.contract.TestStackAdapter` Protocol:
it shells out to ``jest`` (or ``vitest``) to run a subset of tests and to
build a coverage map. Both are **deterministic + subprocess-based** — no
LLM calls, no network.

**Coverage mechanism (S2 CP-2, #919).**  Per-test coverage is not natively
attributed by Jest or Vitest in a single run; but per-test-**file**
isolation *is* obtainable by running the runner scoped to one test file at a
time with coverage. ``coverage_map`` therefore drives the cached, incremental
:class:`~primitives._sdet.stacks.coverage_index.CoverageIndex` (CP-1): for
each stale/new test file it runs ``jest <file> --coverage
--coverageReporters=json --coverageDirectory=<tmp>`` (or the vitest
equivalent), parses the resulting istanbul ``coverage-final.json`` into the
``(source_path, line)`` locations that *one* test file executed, caches it
keyed by content hashes, and materialises a ``measured=True``
:class:`~primitives._sdet.stacks.contract.CoverageMap` from the index. The
index is **base-side** (it measures the current/main tree); a PR consumes it
read-only via the base-side line intersection.

Honest degradation: when the runner is missing, no coverage JSON is
produced, or the staleness sweep would exceed ``s2.max_coverage_runs``, the
method returns ``measured=False`` (empty ``per_test``) so the engine engages
its TM-2 static fallback — it never fabricates per-test attribution.

**Test ID form.**  Jest's ``--json`` reporter uses the test's ``fullName``
(the concatenation of ``ancestorTitles`` + ``title``) and the test file's
absolute path.  The adapter normalises these to the form
``<file-posix-relative-to-root> > <fullName>`` so IDs are stable across
machines (no absolute paths in :class:`~RunResult` or :class:`CoverageMap`
keys), consistent with the :class:`TestSubset` test IDs the caller passes.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

from primitives._sdet.config import SdetConfig
from primitives._sdet.stacks.contract import (
    ChangedSet,
    Confidence,
    CoverageMap,
    RunResult,
    SymbolSet,
    TestResult,
    TestStatus,
    TestSubset,
)
from primitives._sdet.stacks.contract import (
    StackProfile as ContractStackProfile,
)
from primitives._sdet.stacks.coverage_index import (
    CapExceededError,
    CoveredLocations,
    default_index_path,
    load_index,
    materialize_coverage_map,
    refresh,
    save_index,
)

if TYPE_CHECKING:
    from primitives._sdet.stacks.contract import CallGraph

#: Test-file globs scanned to seed the coverage index's test-file id set.
#: Covers jest/vitest's default ``*.test.*`` / ``*.spec.*`` conventions plus
#: ``__tests__/`` directories.
_TEST_FILE_GLOBS: tuple[str, ...] = (
    "**/*.test.js",
    "**/*.test.jsx",
    "**/*.test.ts",
    "**/*.test.tsx",
    "**/*.spec.js",
    "**/*.spec.jsx",
    "**/*.spec.ts",
    "**/*.spec.tsx",
    "**/__tests__/**/*.js",
    "**/__tests__/**/*.jsx",
    "**/__tests__/**/*.ts",
    "**/__tests__/**/*.tsx",
)

#: Directories never scanned for test files (vendor / build output).
_SKIP_DIRS: frozenset[str] = frozenset(
    {"node_modules", "coverage", "dist", "build", ".git", ".next", "out"}
)

_logger = logging.getLogger(__name__)

#: Hard cap on a single jest/vitest invocation (seconds).
_RUN_TIMEOUT_S = 600

NodeLanguage: TypeAlias = Literal["javascript", "typescript"]
NodeFramework: TypeAlias = Literal["jest", "vitest", "mocha", "playwright", "none"]
NodeE2EFramework: TypeAlias = Literal["playwright", "cypress", "none"]


@dataclass(frozen=True)
class StackProfile:
    """Detected JS/TS test stack profile."""

    language: NodeLanguage
    framework: NodeFramework
    framework_version: str | None
    framework_e2e: NodeE2EFramework
    test_glob: str
    coverage_tool: str  # nyc | c8 | vitest-builtin | jest-builtin | none


def _all_deps(pkg: dict[str, object]) -> dict[str, str]:
    """Combine all dep categories into one flat map of name → version-range string."""
    combined: dict[str, str] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        section = pkg.get(key, {})
        if isinstance(section, dict):
            for name, ver in section.items():
                if isinstance(name, str) and isinstance(ver, str):
                    combined[name] = ver
    return combined


_PRIMARY_FRAMEWORKS: list[NodeFramework] = ["vitest", "jest", "mocha"]


def _detect_framework(deps: dict[str, str]) -> tuple[NodeFramework, str | None]:
    """Return (framework, version_string) for the primary test framework."""
    for fw in _PRIMARY_FRAMEWORKS:
        if fw in deps:
            return fw, deps.get(fw)
    if "@playwright/test" in deps:
        return "playwright", deps.get("@playwright/test")
    return "none", None


def _detect_e2e(deps: dict[str, str], framework: NodeFramework) -> NodeE2EFramework:
    """Return the e2e framework when distinct from the primary."""
    if "@playwright/test" in deps and framework != "playwright":
        return "playwright"
    if "cypress" in deps:
        return "cypress"
    return "none"


def _detect_coverage(deps: dict[str, str], framework: NodeFramework) -> str:
    """Return the coverage tool name."""
    if framework == "vitest":
        return "vitest-builtin"
    if framework == "jest":
        return "jest-builtin"
    if "nyc" in deps:
        return "nyc"
    if "c8" in deps:
        return "c8"
    return "none"


def detect_nodejs_stack(project_root: Path) -> StackProfile | None:
    """Detect JS/TS test stack in *project_root*; return None if not a JS/TS project."""
    pkg_json = project_root / "package.json"
    if not pkg_json.is_file():
        return None

    try:
        pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(pkg, dict):
        return None

    deps = _all_deps(pkg)
    language: NodeLanguage = "typescript" if "typescript" in deps else "javascript"
    framework, framework_version = _detect_framework(deps)
    framework_e2e = _detect_e2e(deps, framework)
    coverage_tool = _detect_coverage(deps, framework)

    return StackProfile(
        language=language,
        framework=framework,
        framework_version=framework_version,
        framework_e2e=framework_e2e,
        test_glob="*.test.ts" if language == "typescript" else "*.test.js",
        coverage_tool=coverage_tool,
    )


# ---------------------------------------------------------------------------
# Parsing helpers — pure functions so they are unit-testable without a runner.
# ---------------------------------------------------------------------------


def _make_test_id(file_abs: str, full_name: str, root: Path) -> str:
    """Normalise a jest assertion to a stable, root-relative test id.

    Form: ``<file-posix-relative> > <fullName>``.  Absolute paths in the
    jest JSON reporter are rebased under *root*; paths that cannot be made
    relative (outside the project) are kept as POSIX absolute strings.
    """
    try:
        rel = Path(file_abs).relative_to(root).as_posix()
    except ValueError:
        rel = Path(file_abs).as_posix()
    return f"{rel} > {full_name}"


def parse_jest_json(data: dict[str, object], root: Path) -> tuple[tuple[TestResult, ...], bool]:
    """Parse a jest ``--json`` report dict → ``(results, no_tests_collected)``.

    Returns an empty tuple + ``no_tests_collected=True`` when the report
    contains no assertion results (e.g. the runner exited before collecting).
    """
    test_results_raw = data.get("testResults")
    if not isinstance(test_results_raw, list):
        return (), True

    results: list[TestResult] = []
    for suite in test_results_raw:
        if not isinstance(suite, dict):
            continue
        suite_file: str = suite.get("name", "") or ""
        assertions = suite.get("assertionResults")
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            full_name: str = assertion.get("fullName", "") or ""
            test_id = _make_test_id(suite_file, full_name, root)

            raw_status = assertion.get("status", "")
            status: TestStatus
            if raw_status == "passed":
                status = "passed"
            elif raw_status in {"pending", "todo"}:
                status = "skipped"
            else:
                status = "failed"

            failure_messages = assertion.get("failureMessages")
            failure_text = ""
            if isinstance(failure_messages, list) and failure_messages:
                failure_text = "\n".join(m for m in failure_messages if isinstance(m, str)).strip()

            raw_dur = assertion.get("duration")
            duration: float | None = None
            if isinstance(raw_dur, (int, float)):
                # Jest reports duration in milliseconds; normalise to seconds.
                duration = float(raw_dur) / 1000.0

            results.append(
                TestResult(
                    test_id=test_id,
                    status=status,
                    failure_text=failure_text,
                    duration=duration,
                )
            )

    return tuple(results), not results


def parse_lcov(lcov_text: str, root: Path) -> dict[str, frozenset[int]]:
    """Parse an ``lcov.info`` file → ``{posix-relative-path: frozenset(executed_lines)}``.

    Only ``DA:<line>,<count>`` records with a non-zero count are included
    (lines executed ≥ 1 time).  ``SF:`` records give the source file path;
    relative paths are kept as-is, absolute paths are rebased under *root*.
    Paths that fall outside *root* are retained as POSIX absolute strings
    (e.g. node_modules — callers should filter by their changed set).
    """
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    for raw_line in lcov_text.splitlines():
        line = raw_line.strip()
        if line.startswith("SF:"):
            sf = line[3:]
            try:
                rel = Path(sf).relative_to(root).as_posix()
            except ValueError:
                # Already relative or outside root — keep as-is.
                rel = Path(sf).as_posix() if Path(sf).is_absolute() else sf
            current_file = rel
        elif line.startswith("DA:") and current_file is not None:
            parts = line[3:].split(",", 1)
            if len(parts) == 2:  # noqa: PLR2004 — LCOV DA: has exactly 2 comma-delimited fields
                try:
                    lineno = int(parts[0])
                    count = int(parts[1])
                    if count > 0:
                        result.setdefault(current_file, set()).add(lineno)
                except ValueError:
                    pass
        elif line == "end_of_record":
            current_file = None

    return {path: frozenset(lines) for path, lines in result.items()}


def _rebase_source(sf: str, root: Path) -> str:
    """Rebase an istanbul source path to a POSIX path relative to *root*.

    Absolute paths under *root* become repo-relative; relative paths are kept
    as-is; paths outside *root* are retained as POSIX absolute (callers filter
    by their changed set / index manifest).
    """
    p = Path(sf)
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        return p.as_posix() if p.is_absolute() else sf


def parse_istanbul_json(data: dict[str, object], root: Path) -> frozenset[tuple[str, int]]:
    """Parse an istanbul ``coverage-final.json`` dict → covered ``(path, line)``.

    The istanbul JSON shape is ``{<source-path>: {"statementMap": {idx:
    {"start": {"line": N}, "end": {"line": M}}}, "s": {idx: hit_count}, ...}}``.
    A source line is *covered* when at least one statement spanning it executed
    ≥ 1 time (``s[idx] > 0``).  Each covered statement's ``start.line`` through
    ``end.line`` (inclusive) is recorded, so multi-line statements attribute
    every line they occupy.  Branch / function maps are ignored — statement
    coverage is the line-granularity source of truth here.

    Returns a frozenset of ``(source_path, line)`` pairs (``source_path`` POSIX,
    repo-relative to *root* where possible).  Malformed / partial records are
    skipped rather than raising — an empty frozenset is a valid (no-coverage)
    result the caller treats as UNMEASURED upstream.
    """
    covered: set[tuple[str, int]] = set()
    for raw_path, raw_entry in data.items():
        if not isinstance(raw_path, str) or not isinstance(raw_entry, dict):
            continue
        statement_map = raw_entry.get("statementMap")
        hits = raw_entry.get("s")
        if not isinstance(statement_map, dict) or not isinstance(hits, dict):
            continue
        rel = _rebase_source(raw_path, root)
        for idx, span in statement_map.items():
            count = hits.get(idx)
            if not isinstance(count, (int, float)) or count <= 0:
                continue
            if not isinstance(span, dict):
                continue
            start = span.get("start")
            end = span.get("end")
            start_line = start.get("line") if isinstance(start, dict) else None
            end_line = end.get("line") if isinstance(end, dict) else start_line
            if not isinstance(start_line, int):
                continue
            if not isinstance(end_line, int) or end_line < start_line:
                end_line = start_line
            for ln in range(start_line, end_line + 1):
                covered.add((rel, ln))
    return frozenset(covered)


# ---------------------------------------------------------------------------
# Test-file discovery (seeds the coverage index's id set)
# ---------------------------------------------------------------------------


def discover_test_files(root: Path) -> list[str]:
    """Return repo-relative POSIX paths of every JS/TS test file under *root*.

    Globs the jest/vitest ``*.test.*`` / ``*.spec.*`` conventions plus
    ``__tests__/`` directories, skipping vendor/build dirs and symlinked
    files (defense in depth, mirroring the emitter's symlink discipline).
    Deterministically sorted so the index's id set is stable across machines.
    These ids are the per-test-**file** keys the coverage index is built on,
    matching the ``<file>`` part of :func:`_make_test_id`'s run ids.
    """
    seen: set[str] = set()
    for pattern in _TEST_FILE_GLOBS:
        for path in root.glob(pattern):
            if not path.is_file() or path.is_symlink():
                continue
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            seen.add(rel)
    return sorted(seen)


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


def _find_runner(root: Path, framework: str) -> list[str] | None:
    """Return a list-argv prefix for the JS runner, or None if unavailable.

    Prefers the project-local ``node_modules/.bin/<runner>`` so the project's
    pinned version is used.  Falls back to a PATH lookup.  Returns ``None``
    when neither is resolvable.
    """
    local_bin = root / "node_modules" / ".bin" / framework
    if local_bin.is_file():
        return [str(local_bin)]
    found = shutil.which(framework)
    if found:
        return [found]
    return None


def _invoke(
    argv: list[str],
    root: Path,
    timeout: int = _RUN_TIMEOUT_S,
) -> subprocess.CompletedProcess[str] | None:
    """Run *argv* in *root*; return the completed process or None on failure."""
    try:
        return subprocess.run(  # noqa: S603 -- argv list, no shell, trusted runner
            argv,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _logger.warning("runner invocation failed in %s: %s", root, exc)
        return None


# ---------------------------------------------------------------------------
# Per-test-file isolation measurer (S2 CP-2, #919) — the JS/TS ``MeasureFn``
# ---------------------------------------------------------------------------


def _build_isolation_argv(
    runner_argv: list[str],
    framework: str,
    test_file: str,
    coverage_dir: Path,
) -> list[str]:
    """Build the argv for a coverage run scoped to one *test_file* (istanbul JSON).

    Both jest and vitest accept a positional path-filter argument to scope the
    run to one file, plus ``--coverage`` with the json reporter writing into
    *coverage_dir* (where istanbul drops ``coverage-final.json``). List argv,
    no shell.
    """
    if framework == "vitest":
        return [
            *runner_argv,
            "run",
            test_file,
            "--coverage",
            "--coverage.reporter=json",
            f"--coverage.reportsDirectory={coverage_dir}",
        ]
    return [
        *runner_argv,
        "--coverage",
        "--coverageReporters=json",
        f"--coverageDirectory={coverage_dir}",
        "--forceExit",
        "--testPathPattern",
        # Anchor on the literal file path so only this file runs.
        re.escape(test_file),
    ]


@dataclass(frozen=True)
class NodeCoverageMeasurer:
    """A :class:`~primitives._sdet.stacks.coverage_index.MeasureFn` for JS/TS.

    Given the repo *root* and a test-file id (repo-relative POSIX path), runs
    the detected runner scoped to that **one** test file with coverage,
    parses the resulting istanbul ``coverage-final.json``, and returns the
    ``(source_path, line)`` locations that test file executed.

    Deterministic + subprocess-safe (list argv, no ``shell=True``). Robust to
    a missing runner and to no-coverage output: returns an **empty frozenset**
    (UNMEASURED for that file) rather than fabricating attribution. The index
    layer (CP-1) caches the result keyed by content hashes.
    """

    framework: str

    def __call__(self, root: Path, test_id: str) -> CoveredLocations:
        """Measure covered ``(source_path, line)`` locations for one test file."""
        runner_argv = _find_runner(root, self.framework)
        if runner_argv is None:
            _logger.warning("runner %s not found; %s unmeasured", self.framework, test_id)
            return frozenset()

        with tempfile.TemporaryDirectory(prefix="forge-tm-node-cov-") as tmp:
            cov_dir = Path(tmp)
            argv = _build_isolation_argv(runner_argv, self.framework, test_id, cov_dir)
            completed = _invoke(argv, root)
            if completed is None:
                return frozenset()
            return self._read_istanbul(cov_dir, root)

    @staticmethod
    def _read_istanbul(coverage_dir: Path, root: Path) -> CoveredLocations:
        """Parse ``coverage-final.json`` under *coverage_dir* → covered locations."""
        cov_json = coverage_dir / "coverage-final.json"
        if not cov_json.is_file():
            _logger.warning("istanbul coverage-final.json not found under %s", coverage_dir)
            return frozenset()
        try:
            data = json.loads(cov_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            _logger.warning("failed to parse %s: %s", cov_json, exc)
            return frozenset()
        if not isinstance(data, dict):
            return frozenset()
        return parse_istanbul_json(data, root)


# ---------------------------------------------------------------------------
# Execution adapter (S2 TM-8, #900)
# ---------------------------------------------------------------------------


class NodeStackAdapter:
    """``jest`` / ``vitest`` implementation of the ``TestStackAdapter`` Protocol.

    See the module docstring for the coverage-mechanism rationale: per-test-
    **file** isolation runs feed the cached ``CoverageIndex``, so ``coverage_map``
    returns ``measured=True`` (``side="base"``) in the common case and degrades
    to ``measured=False`` only when the runner is missing, the staleness sweep
    exceeds the cap, or no isolation run produced parseable coverage (S2 CP-2,
    #919). The instance is stateless; all I/O is per-call subprocess work scoped
    to *root*.
    """

    name = "nodejs"

    # -- detection reconciliation ------------------------------------------

    def detect(self, root: Path) -> Confidence:
        """Confidence that *root* is a runnable jest/vitest project.

        Reuses :func:`detect_nodejs_stack`: ``"high"`` for a jest or vitest
        project, ``"medium"`` for a JS/TS project without a detected test
        framework (e.g. mocha, playwright-only), ``"low"`` when not a
        JS/TS project.
        """
        profile = detect_nodejs_stack(root)
        if profile is None:
            return "low"
        if profile.framework in ("jest", "vitest"):
            return "high"
        if profile.framework == "none":
            return "medium"
        return "medium"

    def discover(self, root: Path) -> ContractStackProfile:
        """Project the per-stack profile onto the contract projection."""
        profile = detect_nodejs_stack(root)
        if profile is None:
            return ContractStackProfile(
                language="javascript",
                framework="none",
                coverage_tool="none",
                test_glob="*.test.js",
            )
        return ContractStackProfile(
            language=profile.language,
            framework=profile.framework,
            coverage_tool=profile.coverage_tool,
            test_glob=profile.test_glob,
            framework_version=profile.framework_version,
        )

    # -- execution ---------------------------------------------------------

    def run_tests(self, root: Path, subset: TestSubset) -> RunResult:
        """Run *subset* via jest (or vitest), parsing the ``--json`` report.

        An empty *subset* returns an empty :class:`RunResult` without invoking
        the runner.  When the runner is absent, returns ``no_tests_collected``
        so the caller can fall back gracefully.

        **Test IDs.**  :class:`NodeStackAdapter` uses the form
        ``<file-relative-to-root> > <fullName>`` (see :func:`_make_test_id`).
        Callers must pass subset IDs in this form.  The adapter extracts the
        ``<file>`` part to build the ``--testPathPattern`` filter, so it runs
        only the relevant test file(s) from the subset.
        """
        if subset.is_empty():
            return RunResult(results=(), no_tests_collected=False, exit_code=None)

        profile = detect_nodejs_stack(root)
        framework = profile.framework if profile is not None else "jest"
        if framework not in ("jest", "vitest"):
            framework = "jest"  # default for unknown — runner may not exist

        runner_argv = _find_runner(root, framework)
        if runner_argv is None:
            _logger.warning(
                "Neither local node_modules/.bin/%s nor PATH %s found in %s",
                framework,
                framework,
                root,
            )
            return RunResult(results=(), no_tests_collected=True, exit_code=None)

        with tempfile.TemporaryDirectory(prefix="forge-tm-node-run-") as tmp:
            json_out = Path(tmp) / "jest-results.json"
            argv = self._build_run_argv(runner_argv, framework, subset, json_out)
            completed = _invoke(argv, root)
            if completed is None:
                return RunResult(results=(), no_tests_collected=True, exit_code=None)

            results, no_collected = self._parse_run_output(json_out, root, completed.returncode)
            return RunResult(
                results=results,
                no_tests_collected=no_collected,
                exit_code=completed.returncode,
            )

    def coverage_map(self, root: Path, changed: ChangedSet) -> CoverageMap:
        """Per-test-file coverage via the cached CP-1 index → ``measured=True``.

        Drives the incremental :class:`CoverageIndex` (CP-1):

        1. Load the cached base index (``default_index_path`` / ``load_index``);
           an absent cache is a cold start, not an error.
        2. Discover the project's test files and ``refresh`` the stale/new ones
           via :class:`NodeCoverageMeasurer` (one ``jest <file> --coverage
           --coverageReporters=json`` run each), bounded by
           ``s2.max_coverage_runs``. A :class:`CapExceededError` (over-cap) →
           ``measured=False`` (static fallback), measuring nothing.
        3. Persist the refreshed index (``save_index``) so later PR runs reuse it.
        4. ``materialize_coverage_map(index, changed)`` → a ``measured=True``
           :class:`CoverageMap` tagged ``side="base"`` (the index is base-side).

        Honest degradation to ``measured=False`` (the engine's TM-2 static
        fallback) when: not a jest/vitest project, the runner is missing, the
        staleness sweep exceeds the cap, or no isolation run produced parseable
        coverage. Per-test attribution is never fabricated.
        """
        profile = detect_nodejs_stack(root)
        framework = profile.framework if profile is not None else "jest"
        if framework not in ("jest", "vitest"):
            return CoverageMap(per_test={}, measured=False)

        if _find_runner(root, framework) is None:
            _logger.warning("runner %s not found; coverage_map unmeasured", framework)
            return CoverageMap(per_test={}, measured=False)

        index_path = default_index_path(root, self.name)
        index = load_index(index_path, stack=self.name)
        test_files = discover_test_files(root)
        measurer = NodeCoverageMeasurer(framework=framework)
        max_runs = SdetConfig.from_project(root).s2.max_coverage_runs

        try:
            index = refresh(
                index,
                root,
                test_files=test_files,
                measure=measurer,
                max_runs=max_runs,
            )
        except CapExceededError as exc:
            _logger.warning("%s; coverage_map unmeasured", exc)
            return CoverageMap(per_test={}, measured=False)

        try:
            save_index(index, index_path)
        except OSError as exc:
            # A non-writable cache dir is non-fatal: we still have the in-memory
            # refreshed index for this run.
            _logger.warning("failed to persist coverage index at %s: %s", index_path, exc)

        return materialize_coverage_map(index, changed)

    def call_graph(self, root: Path, symbols: SymbolSet) -> CallGraph | None:  # noqa: ARG002
        """Best-effort downstream call graph — not yet implemented (TM-3)."""
        return None

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _build_run_argv(
        runner_argv: list[str],
        framework: str,
        subset: TestSubset,
        json_out: Path,
    ) -> list[str]:
        """Build the argv list for a test run, scoped to *subset*.

        Subset IDs have the form ``<file> > <fullName>``.  We extract both the
        file part (for ``--testPathPattern``) and the test name part (for
        ``--testNamePattern``) so that only the requested tests run within each
        file — not the entire file.  Name patterns are anchored (``^...$``) and
        joined by ``|``; special regex characters in names are escaped.
        """
        file_paths: set[str] = set()
        test_names: list[str] = []
        for tid in subset.test_ids:
            if " > " in tid:
                file_part, _, name_part = tid.partition(" > ")
                file_paths.add(file_part)
                test_names.append(re.escape(name_part))
            else:
                # Malformed ID — add as-is; the runner will likely collect nothing.
                test_names.append(re.escape(tid))

        if framework == "vitest":
            # vitest run --reporter=json --outputFile=<path> [files...]
            argv = [*runner_argv, "run", "--reporter=json", f"--outputFile={json_out}"]
            argv.extend(sorted(file_paths))
            if test_names:
                name_pattern = "|".join(f"^{n}$" for n in test_names)
                argv.extend(["--testNamePattern", name_pattern])
        else:
            # jest --json --outputFile=<path> --testPathPattern=<path> --testNamePattern=<name>
            argv = [*runner_argv, "--json", f"--outputFile={json_out}", "--forceExit"]
            if file_paths:
                path_pattern = "|".join(re.escape(fp) for fp in sorted(file_paths))
                argv.extend(["--testPathPattern", path_pattern])
            if test_names:
                name_pattern = "|".join(f"^{n}$" for n in test_names)
                argv.extend(["--testNamePattern", name_pattern])
        return argv

    @staticmethod
    def _parse_run_output(
        json_out: Path, root: Path, exit_code: int
    ) -> tuple[tuple[TestResult, ...], bool]:
        """Read and parse the JSON reporter output."""
        # jest exits 1 on failures, 0 on all-pass, but >1 or missing file
        # means something went wrong structurally.
        if not json_out.is_file():
            # No output file — runner likely didn't produce results.
            _logger.warning("jest JSON output file not found at %s", json_out)
            return (), True

        try:
            raw = json_out.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("Failed to parse jest JSON output: %s", exc)
            return (), True

        if not isinstance(data, dict):
            return (), True

        results, no_collected = parse_jest_json(data, root)

        # jest exit code 0 = pass, 1 = failures present, other = error.
        # If no results but exit 0, treat as no_tests_collected=False (empty run).
        if exit_code not in (0, 1) and not results:
            return (), True

        return results, no_collected


def resolve_jsts_framework(project_root: Path) -> str | None:
    """Resolve the JS/TS framework for *project_root* (#1112).

    Precedence — **angular > react > plain nodejs** — so a project that carries both
    ``@angular/core`` and ``react`` deps resolves deterministically to ``angular``
    (`@angular/core` is the definitive framework marker; a React app rarely also
    pulls Angular, and an Angular workspace is unambiguously Angular). Returns the
    stack name (``"angular"`` / ``"react"`` / ``"nodejs"``) or ``None`` when *root*
    is not a JS/TS project at all.
    """
    from primitives._sdet.stacks.angular import detect_angular_stack  # noqa: PLC0415
    from primitives._sdet.stacks.react import detect_react_stack  # noqa: PLC0415

    if detect_angular_stack(project_root) is not None:
        return "angular"
    if detect_react_stack(project_root) is not None:
        return "react"
    if detect_nodejs_stack(project_root) is not None:
        return "nodejs"
    return None


# FLEX-2 (#174): self-register on import so all_stacks() picks us up.
from primitives._sdet.stacks import register_stack, register_unit_meta  # noqa: E402
from primitives._sdet.stacks.contract import UnitTestMeta  # noqa: E402

register_stack("nodejs", detect_nodejs_stack)

# S3 unit-test generation metadata (#1110).
register_unit_meta(
    "nodejs",
    UnitTestMeta(
        test_framework="vitest",
        template_id="vitest",
        stub_raise="throw new Error('not implemented')",
    ),
)

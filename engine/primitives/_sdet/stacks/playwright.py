"""Opt-in Playwright e2e run-adapter for S2 maintenance (#1029).

Implements the :class:`TestStackAdapter` execution contract for Playwright e2e
specs so the S2 verify-by-running green-gate can reach e2e. It is **opt-in**: the
maintainer resolves it explicitly (only under ``--with-e2e`` and when the impact set
carries a ``declared-e2e`` entry); it is registered via ``register_stack`` but
**excluded from the auto-resolution ``_ADAPTER_ORDER``** so it never runs by default
(e2e is slow/flaky). ``coverage_map``/``call_graph`` are no-ops -- e2e impact comes
from the declared map, not from coverage.
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING, Any

from primitives._sdet.stacks.contract import (
    CoverageMap,
    RunResult,
    StackProfile,
    TestResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._sdet.stacks.contract import (
        CallGraph,
        ChangedSet,
        Confidence,
        SymbolSet,
        TestSubset,
    )

#: Hard cap on a single playwright invocation (seconds).
_DEFAULT_TIMEOUT = 600

#: Playwright result statuses that mean the spec did not pass.
_FAIL_STATUSES = frozenset({"failed", "timedOut", "interrupted"})


def detect_playwright_stack(root: Path) -> StackProfile | None:
    """Return a profile when *root* has a ``playwright.config.*`` file.

    The config presence is the detection heuristic (a Playwright project always has
    one); the ``@playwright/test`` dep isn't separately required.
    """
    has_config = any(
        (root / name).is_file()
        for name in (
            "playwright.config.ts",
            "playwright.config.js",
            "playwright.config.mjs",
        )
    )
    if not has_config:
        return None
    return StackProfile(
        language="nodejs",
        framework="playwright",
        coverage_tool="none",
        test_glob="**/*.spec.ts",
    )


def _walk_specs(suite: dict[str, Any], inherited_file: str) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(file, spec)`` for every spec under *suite*, recursing nested suites.

    A real Playwright report nests ``suites[].suites[]`` for each ``describe()``
    block; the specs live in the leaf suites, so a flat top-level read would miss
    everything inside a ``describe()``. A child suite inherits its parent's ``file``
    when it doesn't carry its own.
    """
    file = suite.get("file") or inherited_file or suite.get("title", "")
    out: list[tuple[str, dict[str, Any]]] = []
    for spec in suite.get("specs", []):
        out.append((spec.get("file") or file, spec))
    for child in suite.get("suites", []):
        out.extend(_walk_specs(child, file))
    return out


def parse_playwright_report(report: dict[str, Any]) -> list[TestResult]:
    """Collapse the Playwright JSON reporter to one :class:`TestResult` per spec file.

    A file is ``failed`` if any of its specs failed/timed-out/was interrupted (with
    the first error message), else ``passed``. Selection + reporting are file-level
    (Playwright's natural granularity for ``playwright test <file>``). Nested suites
    (``describe()`` blocks) are walked recursively.
    """
    by_file: dict[str, dict[str, Any]] = {}
    for suite in report.get("suites", []):
        for f, spec in _walk_specs(suite, ""):
            if not f:
                continue
            bucket = by_file.setdefault(f, {"status": "passed", "failure": "", "duration": 0.0})
            for t in spec.get("tests", []):
                for r in t.get("results", []):
                    bucket["duration"] += float(r.get("duration", 0)) / 1000.0
                    if r.get("status") in _FAIL_STATUSES:
                        bucket["status"] = "failed"
                        err = (r.get("error") or {}).get("message", "")
                        if err and not bucket["failure"]:
                            bucket["failure"] = err
    return [
        TestResult(
            test_id=f,
            status=b["status"],
            failure_text=b["failure"],
            duration=b["duration"],
        )
        for f, b in sorted(by_file.items())
    ]


class PlaywrightAdapter:
    """Playwright e2e execution adapter (opt-in; see module docstring)."""

    name = "playwright"

    def detect(self, root: Path) -> Confidence:
        return "high" if detect_playwright_stack(root) is not None else "low"

    def discover(self, root: Path) -> StackProfile:
        return detect_playwright_stack(root) or StackProfile(
            language="nodejs",
            framework="none",
            coverage_tool="none",
            test_glob="**/*.spec.ts",
        )

    def coverage_map(self, root: Path, changed: ChangedSet) -> CoverageMap:  # noqa: ARG002
        # e2e impact is map-driven, not coverage-driven (protocol no-op).
        return CoverageMap(per_test={}, measured=False)

    def run_tests(self, root: Path, subset: TestSubset) -> RunResult:
        if subset.is_empty():
            return RunResult(results=(), no_tests_collected=True, exit_code=0)
        spec_files = sorted(subset.test_ids)
        try:
            proc = subprocess.run(  # noqa: S603 - args are repo-relative spec paths
                ["npx", "playwright", "test", *spec_files, "--reporter=json"],  # noqa: S607
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=_DEFAULT_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return RunResult(
                results=(
                    TestResult(
                        test_id=spec_files[0],
                        status="error",
                        failure_text=f"playwright not runnable: {exc}",
                        duration=0.0,
                    ),
                ),
                no_tests_collected=True,
                exit_code=1,
            )
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return RunResult(results=(), no_tests_collected=True, exit_code=proc.returncode)
        results = tuple(parse_playwright_report(report))
        return RunResult(
            results=results,
            no_tests_collected=not results,
            exit_code=proc.returncode,
        )

    def call_graph(self, root: Path, symbols: SymbolSet) -> CallGraph | None:  # noqa: ARG002
        return None


from primitives._sdet.stacks import register_stack  # noqa: E402

# Opt-in (#1029): registered so it can be resolved on demand, but deliberately NOT
# added to _ADAPTER_ORDER so resolve_adapter never auto-selects it.
register_stack("playwright", detect_playwright_stack)

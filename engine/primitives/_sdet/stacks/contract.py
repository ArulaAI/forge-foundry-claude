"""The ``TestStackAdapter`` execution contract (S2 TM-1, #893).

This module homes the *execution* surface for the S2 ``test_maintainer``
closed-loop maintainer (design spec
``docs/superpowers/specs/2026-06-19-test-maintainer-closed-loop-design.md``
§4, §6, §8). The per-stack modules in this package (``python.py``,
``nodejs.py``, ``java.py``) already supply **detection** — framework /
coverage-plugin sniffing and a per-stack :class:`StackProfile`. The
:class:`TestStackAdapter` Protocol layered here adds the four execution
methods the closed loop needs:

- :meth:`~TestStackAdapter.detect` / :meth:`~TestStackAdapter.discover` —
  thin reconciliations over the existing ``detect_<lang>_stack`` functions.
- :meth:`~TestStackAdapter.coverage_map` — run the suite with per-test
  coverage and return a *test → set of (file, line)* reverse index, so a
  later phase can intersect the diff's changed lines → impacted tests
  (spec §4, coverage-primary hybrid).
- :meth:`~TestStackAdapter.run_tests` — execute a given subset of tests
  and return per-test pass/fail + captured failure output + duration
  (spec §6, the green-gate verify loop).
- :meth:`~TestStackAdapter.call_graph` — best-effort downstream caller
  graph; **may return ``None``** (a real implementation lands in TM-3).

All adapters are **deterministic + subprocess-based** — they shell out to
real test runners (``pytest``, ``jest``/``vitest``, ``maven``/``gradle``).
NO LLM calls, no network. This contract module itself defines only the
Protocol + the shared typed models; the concrete adapters live in the
per-stack modules.

The shared :class:`StackProfile` here is a *contract-level* projection
(``language`` / ``framework`` / ``coverage_tool`` / ``test_glob``) — the
common fields every per-stack ``StackProfile`` already carries. Adapters
build one from their richer per-stack profile in
:meth:`~TestStackAdapter.discover`; callers in the engine only ever depend
on the projection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, TypeAlias, runtime_checkable

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Confidence — closed Literal, mirroring the discovery flavor-pack ladder
# (``primitives._shared.discovery.models.Confidence``) so a per-stack
# ``detect`` reads the same way as a flavor ``detect``. Redeclared locally
# rather than imported so ``_sdet`` does not take a structural dependency on
# the M-tier discovery models.
# ---------------------------------------------------------------------------
Confidence: TypeAlias = Literal["high", "medium", "low"]

#: Per-test outcome status, mirroring pytest / jest / JUnit report verbs.
TestStatus: TypeAlias = Literal["passed", "failed", "error", "skipped"]


# ---------------------------------------------------------------------------
# Lightweight typed wrappers for the engine's set-shaped inputs. They are
# frozen dataclasses (not bare ``set`` aliases) so the call sites read
# self-documenting and the contract can grow per-set metadata later without
# a signature churn.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChangedSet:
    """A diff's changed lines, keyed by repo-relative file path.

    ``lines`` maps a ``path`` (POSIX, repo-relative) to the frozenset of
    1-based **new-side** line numbers the diff touched in that file (the
    added/changed lines in the working tree). ``base_lines`` is the additive
    companion (S2 CP-1, #918): the frozenset of 1-based **base-side (old)**
    line numbers each hunk removed/replaced — the coordinate space the cached
    base coverage index is indexed in. Built upstream from ``git diff`` (the
    hunk header ``@@ -old,+new @@`` carries both ranges); consumed by
    :meth:`TestStackAdapter.coverage_map` and the read-only coverage-index
    selector.
    """

    lines: dict[str, frozenset[int]] = field(default_factory=dict)
    base_lines: dict[str, frozenset[int]] = field(default_factory=dict)

    def paths(self) -> frozenset[str]:
        """The changed file paths (union of new-side and base-side)."""
        return frozenset(self.lines) | frozenset(self.base_lines)

    def is_empty(self) -> bool:
        """True when no file/line was recorded as changed (either side)."""
        return not self.lines and not self.base_lines

    def lines_for(self, path: str, *, side: Literal["new", "base"] = "new") -> frozenset[int]:
        """The lines to intersect against for *path*, for the requested *side*.

        ``side="new"`` (the default) returns **new-side** lines — the
        coordinate space the live ``measured=True`` Python path measures (the
        current tree). ``side="base"`` returns **base-side (old)** lines — the
        coordinate space the cached base coverage index is indexed in (consumed
        only by the read-only index selector, never by a live map).

        The two coordinate systems are kept strictly separate: a base-side
        request never silently falls back to new-side lines (or vice versa),
        so a live new-side map can never be intersected against base lines.
        """
        if side == "base":
            return self.base_lines.get(path, frozenset())
        return self.lines.get(path, frozenset())


@dataclass(frozen=True)
class SymbolSet:
    """A set of fully-qualified symbol names (funcs/methods/classes).

    Seeds :meth:`TestStackAdapter.call_graph`. The exact qualification
    convention is stack-defined (e.g. ``pkg.mod:func`` for Python); callers
    treat the strings opaquely.
    """

    symbols: frozenset[str] = field(default_factory=frozenset)

    def is_empty(self) -> bool:
        """True when no symbol is present."""
        return not self.symbols


@dataclass(frozen=True)
class TestSubset:
    """A subset of tests to execute, by stack-native test id.

    For pytest these are node-ids (``tests/test_x.py::test_y``); other
    stacks use their own id form. An empty subset is a no-op that
    :meth:`TestStackAdapter.run_tests` reports as zero results (it must not
    fall back to running the whole suite).
    """

    # Tell pytest this is not a test class despite the ``Test`` prefix.
    __test__: ClassVar[bool] = False

    test_ids: frozenset[str] = field(default_factory=frozenset)

    def is_empty(self) -> bool:
        """True when the subset selects no tests."""
        return not self.test_ids


# ---------------------------------------------------------------------------
# Result models — frozen, hashable-where-possible. ``CoverageMap`` is the
# reverse index; ``RunResult`` carries per-test outcomes.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageMap:
    """Reverse index: test id → the ``(path, line)`` pairs it executed.

    ``per_test`` maps a stack-native test id to the frozenset of executed
    ``(path, line)`` locations (``path`` POSIX, repo-relative). A later
    phase intersects these with a :class:`ChangedSet` to find the
    empirically-impacted tests (spec §4). ``measured`` is False when no
    per-test coverage could be produced (e.g. the coverage tool was
    absent) — the engine then falls back to the static graph + the model.

    ``side`` (S2 CP-2, #919) records which coordinate space ``per_test``'s
    line numbers live in, so the impact engine intersects against the matching
    side of the :class:`ChangedSet`:

    - ``"new"`` (default) — a **live** map measured against the current tree
      (the Python adapter's ``_read_coverage``); intersected against the diff's
      new-side lines.
    - ``"base"`` — a map materialised from the cached **base-indexed** coverage
      index (JS/TS + Java, via ``materialize_coverage_map``); intersected
      against the diff's base-side (old) lines.

    The two are kept strictly separate: a ``side="new"`` map is never
    intersected against base lines and vice versa.
    """

    per_test: dict[str, frozenset[tuple[str, int]]] = field(default_factory=dict)
    measured: bool = True
    side: Literal["new", "base"] = "new"

    def tests_touching(self, path: str, line: int) -> frozenset[str]:
        """The test ids whose execution covered ``(path, line)``."""
        loc = (path, line)
        return frozenset(tid for tid, locs in self.per_test.items() if loc in locs)

    def impacted_by(
        self, changed: ChangedSet, *, side: Literal["new", "base"] = "new"
    ) -> frozenset[str]:
        """Tests whose executed lines intersect *changed*'s lines on *side*.

        Defaults to ``side="new"`` — the coordinate space a live
        ``measured=True`` map is produced in (e.g. the Python adapter's
        ``_read_coverage``, whose ``(path, line)`` pairs are current-tree /
        new-side). Every live caller (including the impact engine's
        ``compute_impacted``) is genuinely unchanged: a live map is *never*
        intersected against base-side lines.

        The cached **base-indexed** coverage index is selected with
        ``side="base"`` — done only via the read-only index selector
        (``coverage_index.materialize_coverage_map`` + the PR-time
        ``coverage_map``), the single place base-side lines are consumed.
        """
        hits: set[str] = set()
        for tid, locs in self.per_test.items():
            for path, line in locs:
                if line in changed.lines_for(path, side=side):
                    hits.add(tid)
                    break
        return frozenset(hits)


@dataclass(frozen=True)
class TestResult:
    """A single test's outcome from a :meth:`TestStackAdapter.run_tests` run."""

    # Tell pytest this is not a test class despite the ``Test`` prefix.
    __test__: ClassVar[bool] = False

    test_id: str
    status: TestStatus
    #: Captured failure / error text (traceback, assertion message). Empty
    #: for passed / skipped tests.
    failure_text: str = ""
    #: Wall-clock duration in seconds; ``None`` when the runner did not
    #: report a per-test duration.
    duration: float | None = None


@dataclass(frozen=True)
class RunResult:
    """The outcome of executing a :class:`TestSubset`."""

    results: tuple[TestResult, ...] = ()
    #: True when the runner collected zero tests for the requested subset
    #: (e.g. a node-id that no longer resolves). Distinct from "all passed".
    no_tests_collected: bool = False
    #: Raw runner exit code, for callers that branch on it. ``None`` when
    #: the run was short-circuited (empty subset) without invoking a runner.
    exit_code: int | None = None

    @property
    def passed(self) -> bool:
        """True when at least one test ran and none failed or errored."""
        if not self.results:
            return False
        return all(r.status in ("passed", "skipped") for r in self.results)

    def failures(self) -> tuple[TestResult, ...]:
        """The failed / errored results."""
        return tuple(r for r in self.results if r.status in ("failed", "error"))


@dataclass(frozen=True)
class CallEdge:
    """A directed caller → callee edge between two symbols."""

    caller: str
    callee: str


@dataclass(frozen=True)
class CallGraph:
    """A best-effort symbol call graph (downstream callers).

    ``edges`` are ``caller → callee`` pairs. The type is defined now;
    population is best-effort and lands in TM-3 — until then
    :meth:`TestStackAdapter.call_graph` may return ``None``.
    """

    edges: tuple[CallEdge, ...] = ()

    def callers_of(self, symbol: str) -> frozenset[str]:
        """Direct callers of *symbol*."""
        return frozenset(e.caller for e in self.edges if e.callee == symbol)


@dataclass(frozen=True)
class UnitTestMeta:
    """S3 ``test-from-spec`` unit-test generation metadata for a stack (#1110).

    Declared per stack in the ``_sdet`` registry so S3 and S4 share one source
    of truth for *how* to generate a unit test in that stack:

    - ``test_framework`` — the framework the generated tests target
      (``pytest`` / ``junit5`` / ``vitest`` / ``rtl`` / ``testbed`` / ``xunit``).
    - ``template_id`` — the ``skill/references/stack_templates/`` skeleton id S3
      fills with the AAA body.
    - ``stub_raise`` — the collection-green "not implemented" statement used to
      stub the unit-under-test so the suite collects/compiles while the tests
      still fail (e.g. ``raise NotImplementedError`` /
      ``throw new Error('not implemented')`` / ``throw new
      UnsupportedOperationException();``) (#1110, consumed by the collection-green
      mode in #1027 D1).
    """

    test_framework: str
    template_id: str
    stub_raise: str


@dataclass(frozen=True)
class DataBackendMeta:
    """Data-backend (database engine) metadata for the S4 container layer (#1124).

    Declared per backend in the ``_sdet`` registry so ``sdet_architect`` (DT-1.1)
    and ``test_scaffold`` (S4) share one source of truth for *which* data backends
    exist, *how* to detect them, and *how* to containerize them. Mirrors
    :class:`UnitTestMeta`: it holds *coordinates* (import path + class identifiers),
    not rendered harness bodies — the bodies live in ``test_scaffold``.

    - ``name`` — backend id (``"postgres"`` / ``"mysql"`` / ``"mssql"`` /
      ``"oracle"`` / ``"mongodb"`` / ``"dynamodb"``).
    - ``signals`` — dependency-driver substrings matched (lowercased) against a
      project's manifest text to detect the backend.
    - ``kind`` — selects the connection-assert idiom the container renderer emits
      (``"sql"`` → open connection + ``SELECT 1``; ``"mongodb"`` → client ping;
      ``"dynamodb"`` → ``list_tables`` against the local endpoint).
    - ``default_image`` — the default Testcontainers image (overridable via the
      ``container_image`` fillin).
    - ``port`` — the backend's exposed port.
    - ``py_module`` — Python Testcontainers import path + class
      (e.g. ``"testcontainers.mysql.MySqlContainer"``).
    - ``java_class`` — Java Testcontainers class simple name
      (e.g. ``"MySQLContainer"``).
    """

    name: str
    signals: tuple[str, ...]
    kind: Literal["sql", "mongodb", "dynamodb"]
    default_image: str
    port: int
    py_module: str
    java_class: str


@dataclass(frozen=True)
class StackProfile:
    """Contract-level projection of a per-stack detected profile.

    The common fields every per-stack ``StackProfile`` already exposes.
    Adapters build one in :meth:`TestStackAdapter.discover` from their
    richer per-stack dataclass; engine callers depend only on this shape.
    """

    language: str
    framework: str
    coverage_tool: str
    test_glob: str
    framework_version: str | None = None


@runtime_checkable
class TestStackAdapter(Protocol):
    """Per-stack execution contract for the S2 closed-loop maintainer.

    Implementations are deterministic and subprocess-based: they shell out
    to the stack's real test runner. See the module docstring for the
    coverage-primary-hybrid rationale.
    """

    #: Stack name, matching the detection registry key (``"python"`` …).
    name: str

    def detect(self, root: Path) -> Confidence:
        """Confidence that *root* is this adapter's stack.

        Reconciles with the per-stack ``detect_<lang>_stack`` detector:
        returns ``"high"``/``"medium"`` when that detector recognises the
        project and ``"low"`` otherwise.
        """
        ...

    def discover(self, root: Path) -> StackProfile:
        """Detected framework / layout / coverage tool for *root*.

        Wraps the per-stack detector, projecting its richer profile onto
        the contract-level :class:`StackProfile`.
        """
        ...

    def coverage_map(self, root: Path, changed: ChangedSet) -> CoverageMap:
        """Run the suite with per-test coverage → test → executed-lines.

        *changed* scopes the measurement (the adapter may narrow coverage
        to the changed files for cost). Returns a :class:`CoverageMap`
        whose ``measured`` flag is False when no per-test coverage could be
        produced.
        """
        ...

    def run_tests(self, root: Path, subset: TestSubset) -> RunResult:
        """Execute *subset* and return per-test pass/fail + failures.

        An empty *subset* returns an empty :class:`RunResult` (no runner is
        invoked); a subset that collects nothing sets
        ``no_tests_collected``.
        """
        ...

    def call_graph(self, root: Path, symbols: SymbolSet) -> CallGraph | None:
        """Best-effort downstream call graph seeded by *symbols*.

        May return ``None`` — a real implementation lands in TM-3.
        """
        ...


# Tell pytest the ``Test``-prefixed Protocol is not a test class. Assigned
# *after* the class body so it is not a structural protocol member (which
# would force every implementer to declare it).
TestStackAdapter.__test__ = False  # type: ignore[attr-defined]


__all__ = [
    "CallEdge",
    "CallGraph",
    "ChangedSet",
    "Confidence",
    "CoverageMap",
    "DataBackendMeta",
    "RunResult",
    "StackProfile",
    "SymbolSet",
    "TestResult",
    "TestStackAdapter",
    "TestStatus",
    "TestSubset",
    "UnitTestMeta",
]

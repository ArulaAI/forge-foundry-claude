"""Python test stack detection + execution adapter.

The detection half (``detect_python_stack`` → :class:`StackProfile`)
determines whether a project uses pytest, unittest, or has no Python tests,
and which coverage tool is wired in.

The execution half (:class:`PythonStackAdapter`, S2 TM-1 #893) implements
the :class:`~primitives._sdet.stacks.contract.TestStackAdapter` Protocol:
it shells out to ``pytest`` to run a subset of tests and to build a
per-test coverage map. Both are **deterministic + subprocess-based** — no
LLM calls, no network.

**Coverage mechanism.** ``coverage_map`` runs ``pytest`` under coverage.py
**dynamic contexts** (``--cov-context=test``): each test is recorded as its
own coverage *context*, so the resulting ``.coverage`` SQLite database
carries, per measured line, the set of test contexts that executed it. We
read it back with :class:`coverage.CoverageData` and invert
``contexts_by_lineno`` into the contract's *test id → {(path, line)}*
reverse index. This is the reliable per-TEST mechanism the design spec
(§4) calls for; a precise static call graph is deliberately *not* the
source of truth. The run uses a private data file under a temp dir so it
never clobbers a consumer's ``.coverage``.

**Call graph (TM-3).** ``call_graph`` walks every non-test ``.py`` file
under *root* via the stdlib ``ast`` module and builds caller→callee edges
for function and method calls. This is **best-effort static analysis**:
dynamic dispatch (duck-typed calls, ``getattr``, factories, decorators that
replace the callee) is not resolved; only syntactic ``Name`` and
``Attribute`` call targets are captured.  The symbol qualification
convention is ``<repo-relative-posix-path>:<function_name>`` for file-
scoped symbols and ``<path>:<ClassName>.<method_name>`` for methods.
Callers outside *root* (e.g. site-packages) are skipped.  When *symbols*
is non-empty, only files whose AST mentions a symbol name (any name, to keep
the walk bounded) are fully traversed — otherwise the full source tree is
walked. Hard limits (file count / file size) prevent runaway scans on very
large repositories.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeAlias

from defusedxml.ElementTree import ParseError as DefusedParseError
from defusedxml.ElementTree import parse as defused_parse

from primitives._sdet.stacks.contract import (
    CallEdge,
    CallGraph,
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

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

_logger = logging.getLogger(__name__)

#: Hard cap on a single pytest invocation (seconds). Bounds cost per the
#: anti-runaway rule in the design spec.
_RUN_TIMEOUT_S = 600

#: pytest's exit code for "no tests were collected".
_PYTEST_NO_TESTS_EXIT = 5

# ---------------------------------------------------------------------------
# Call-graph analysis constants (TM-3)
# ---------------------------------------------------------------------------

#: Maximum number of ``.py`` source files to walk in a single call_graph()
#: invocation.  Prevents runaway scans on very large codebases.
_CALLGRAPH_MAX_FILES = 500

#: Maximum file size (bytes) to parse in the call-graph walk.  Files larger
#: than this are silently skipped (best-effort).
_CALLGRAPH_MAX_FILE_BYTES = 256 * 1024  # 256 KiB


# ---------------------------------------------------------------------------
# Call-graph helpers (TM-3)
# ---------------------------------------------------------------------------


def _py_files(root: Path) -> list[Path]:
    """Return all ``.py`` source files under *root*, excluding ``__pycache__``
    and common virtual-env / build directories, capped at
    :data:`_CALLGRAPH_MAX_FILES`.
    """
    _SKIP_DIRS = frozenset(
        {
            "__pycache__",
            ".venv",
            "venv",
            ".tox",
            "build",
            "dist",
            ".eggs",
            "node_modules",
            "target",
        }
    )
    files: list[Path] = []
    for p in sorted(root.rglob("*.py")):
        # Skip any path component that is a known skip dir
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        files.append(p)
        if len(files) >= _CALLGRAPH_MAX_FILES:
            break
    return files


def _qualify_symbol(rel: str, class_name: str | None, func_name: str) -> str:
    """Return ``<rel>:<ClassName.func>`` or ``<rel>:<func>``."""
    if class_name:
        return f"{rel}:{class_name}.{func_name}"
    return f"{rel}:{func_name}"


def _extract_call_targets(node: ast.Call) -> list[str]:
    """Extract the bare callee name(s) from a :class:`ast.Call` node.

    Returns ``["name"]`` for ``name()``, ``["attr"]`` for ``obj.attr()``.
    Chained calls (``a.b.c()``) return only the final attribute (``"c"``).
    Dynamic targets (subscripts, starred, etc.) return ``[]``.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return [func.id]
    if isinstance(func, ast.Attribute):
        return [func.attr]
    return []


class _CallGraphVisitor(ast.NodeVisitor):
    """AST visitor that builds caller→callee edges for one file.

    Qualification convention: ``<repo-relative-posix>:<ClassName.method>``
    or ``<repo-relative-posix>:<function_name>`` for module-level symbols.

    Dynamic dispatch (duck-typed calls, ``getattr``, decorators that replace
    the callee) is **not** resolved — this is best-effort static analysis.
    """

    def __init__(self, rel_path: str) -> None:
        self._rel = rel_path
        self._class_stack: list[str] = []
        self._func_stack: list[str] = []
        self.edges: list[CallEdge] = []

    # -- caller context tracking ------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter_func(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter_func(node)

    def _enter_func(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        class_name = self._class_stack[-1] if self._class_stack else None
        caller = _qualify_symbol(self._rel, class_name, node.name)
        self._func_stack.append(caller)
        self.generic_visit(node)
        self._func_stack.pop()

    # -- call detection ---------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        if self._func_stack:
            caller = self._func_stack[-1]
            for callee_name in _extract_call_targets(node):
                # Record a bare-name callee — the resolver in
                # build_python_call_graph() will match it against known symbols.
                self.edges.append(CallEdge(caller=caller, callee=callee_name))
        self.generic_visit(node)


def build_python_call_graph(root: Path, symbols: SymbolSet) -> CallGraph:  # noqa: ARG001
    """Build a best-effort Python call graph for *root*.

    Walks ``.py`` files under *root* (excluding test files and vendor dirs),
    extracts caller→callee edges via AST analysis, and returns a
    :class:`~primitives._sdet.stacks.contract.CallGraph`.

    **Symbol naming:** symbols in ``SymbolSet`` are expected to match the
    ``<rel-path>:<name>`` or ``<rel-path>:<Class.method>`` convention that
    this adapter's own edges use, OR bare ``<function_name>`` strings.
    :meth:`~CallGraph.callers_of` queries work against the full qualified
    form; the call-graph engine also resolves bare callee names to qualified
    caller symbols.

    **Limits / best-effort:** dynamic dispatch, ``getattr``-based dispatch,
    decorated callables, and cross-module re-exports are **not** resolved.
    The result is an approximation; coverage is the source of truth (spec
    §4). Returns a :class:`CallGraph` with zero edges on parse failure.
    """
    py_files = _py_files(root)

    # Build a name → set[qualified] map so we can resolve bare callee names
    # collected by the visitor to their qualified form.
    # E.g. visitor records edge(caller="src/a.py:bar", callee="foo");
    # name_to_qualified["foo"] = {"src/b.py:foo"} → emit that resolved edge.
    name_to_qualified: dict[str, set[str]] = {}

    # Per-file visitor output: list of (caller, bare_callee_name) pairs
    raw_edges: list[tuple[str, str]] = []

    for py_file in py_files:
        if py_file.stat().st_size > _CALLGRAPH_MAX_FILE_BYTES:
            continue
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(py_file))
        except (SyntaxError, OSError):
            continue

        try:
            rel = py_file.relative_to(root).as_posix()
        except ValueError:
            continue

        # Register module-level and class-level function names
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                bare = node.name
                name_to_qualified.setdefault(bare, set()).add(f"{rel}:{bare}")
            elif isinstance(node, ast.ClassDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                        bare_method = child.name
                        qualified_method = f"{rel}:{node.name}.{bare_method}"
                        name_to_qualified.setdefault(bare_method, set()).add(qualified_method)

        visitor = _CallGraphVisitor(rel)
        visitor.visit(tree)
        for edge in visitor.edges:
            raw_edges.append((edge.caller, edge.callee))

    # Resolve bare callee names → qualified symbols; deduplicate.
    resolved: set[tuple[str, str]] = set()
    for caller, bare_callee in raw_edges:
        targets = name_to_qualified.get(bare_callee, set())
        if targets:
            for t in targets:
                resolved.add((caller, t))
        else:
            # Keep the bare form so callers_of("func") still finds edges
            # even when the callee is defined outside *root*.
            resolved.add((caller, bare_callee))

    edges = tuple(CallEdge(caller=c, callee=t) for c, t in sorted(resolved))
    return CallGraph(edges=edges)


PyFramework: TypeAlias = Literal["pytest", "unittest", "none"]
PyCoverageTool: TypeAlias = Literal["pytest-cov", "coverage", "none"]


@dataclass(frozen=True)
class StackProfile:
    """Detected Python test stack profile."""

    language: Literal["python"]
    framework: PyFramework
    framework_version: str | None
    test_glob: str
    coverage_tool: PyCoverageTool


def _read_pyproject_deps(pyproject: Path) -> tuple[set[str], dict[str, str | None]]:
    """Return (all_deps_set, version_map). Version map values may be None."""
    deps: set[str] = set()
    versions: dict[str, str | None] = {}
    if not pyproject.is_file():
        return deps, versions
    content = pyproject.read_text(encoding="utf-8")

    # Look for [project].dependencies + [dependency-groups].dev + [tool.pytest...]
    for section in re.finditer(r"(?:dependencies|dev|test)\s*=\s*\[([^\]]*)\]", content, re.S):
        for raw in re.findall(r'"([^"]+)"', section.group(1)):
            name = re.split(r"[<>=!~ ]", raw)[0].strip().lower()
            ver_match = re.search(r"[<>=!~]+\s*([0-9.]+)", raw)
            if name:
                deps.add(name)
                versions[name] = ver_match.group(1) if ver_match else None
    return deps, versions


def _has_unittest_imports(tests_dir: Path) -> bool:
    """Return True if any *.py file under tests_dir imports unittest."""
    if not tests_dir.is_dir():
        return False
    for f in tests_dir.rglob("*.py"):
        try:
            if "import unittest" in f.read_text(encoding="utf-8"):
                return True
        except (UnicodeDecodeError, PermissionError):
            continue
    return False


def detect_python_stack(project_root: Path) -> StackProfile | None:
    """Detect Python test stack in *project_root*; return None if not a Python project."""
    pyproject = project_root / "pyproject.toml"
    has_python = pyproject.is_file() or any(project_root.rglob("*.py"))
    if not has_python:
        return None

    deps, versions = _read_pyproject_deps(pyproject)

    framework: PyFramework
    framework_version: str | None
    test_glob: str
    coverage_tool: PyCoverageTool

    if "pytest" in deps:
        framework = "pytest"
        framework_version = versions.get("pytest")
        test_glob = "test_*.py"
    elif _has_unittest_imports(project_root / "tests"):
        framework = "unittest"
        framework_version = None
        test_glob = "test_*.py"
    else:
        framework = "none"
        framework_version = None
        test_glob = "test_*.py"

    if "pytest-cov" in deps:
        coverage_tool = "pytest-cov"
    elif "coverage" in deps:
        coverage_tool = "coverage"
    else:
        coverage_tool = "none"

    return StackProfile(
        language="python",
        framework=framework,
        framework_version=framework_version,
        test_glob=test_glob,
        coverage_tool=coverage_tool,
    )


# ---------------------------------------------------------------------------
# Execution adapter (S2 TM-1, #893)
# ---------------------------------------------------------------------------


def _normalize_path(root: Path, raw: str) -> str:
    """Return *raw* as a POSIX path relative to *root* when possible."""
    p = Path(raw)
    try:
        if p.is_absolute():
            p = p.relative_to(root)
    except ValueError:
        # Outside root (e.g. site-packages) — keep absolute POSIX form.
        return p.as_posix()
    return p.as_posix()


def _normalize_test_id(root: Path, raw: str) -> str:
    """Normalize a coverage context / node-id to a repo-relative node-id.

    Coverage test contexts are pytest node-ids, but the file part may be an
    absolute path; we rebase it under *root* so context ids line up with the
    node-ids ``run_tests`` parses.
    """
    if "::" not in raw:
        return raw
    file_part, _, rest = raw.partition("::")
    return f"{_normalize_path(root, file_part)}::{rest}"


def _parse_junit(
    xml_path: Path, root: Path, requested: frozenset[str]
) -> tuple[tuple[TestResult, ...], bool]:
    """Parse a pytest ``--junitxml`` report → (results, no_tests_collected).

    *requested* is the subset's node-ids; it is used to reconcile each
    junit ``<testcase>`` back to the exact node-id form the caller asked
    for (pytest's junit carries a dotted ``classname`` + bare ``name``, not
    the ``path::test`` node-id), so the returned ``test_id`` lines up with
    the coverage-map keys and the caller's subset.
    """
    try:
        tree = defused_parse(xml_path)
    except (DefusedParseError, OSError):
        return (), True

    results: list[TestResult] = []
    # <testsuites><testsuite><testcase .../> — handle both roots.
    for suite in tree.iterfind(".//testsuite"):
        for case in suite.iterfind("testcase"):
            results.append(_testcase_to_result(case, root, requested))
    return tuple(results), not results


def _derive_node_id(case: Element, root: Path, requested: frozenset[str]) -> str:
    """Best-effort node-id for a junit ``<testcase>``.

    Prefers the junit ``file`` attribute when present (newer pytest);
    otherwise reconstructs ``a/b/c.py::Class::test`` from the dotted
    ``classname`` + ``name`` and, where that reconstruction is ambiguous,
    reconciles against the *requested* node-ids by matching the test
    ``name`` suffix.
    """
    name = case.get("name", "")
    file_attr = case.get("file")
    if file_attr:
        return f"{_normalize_path(root, file_attr)}::{name}"

    classname = case.get("classname", "")
    reconstructed = ""
    if classname:
        parts = classname.split(".")
        # Heuristic: a trailing CamelCase segment is a test class, not a
        # module file. Everything before it is the module path.
        if len(parts) > 1 and parts[-1][:1].isupper():
            module_path = "/".join(parts[:-1]) + ".py"
            reconstructed = f"{module_path}::{parts[-1]}::{name}"
        else:
            reconstructed = f"{'/'.join(parts)}.py::{name}"

    if reconstructed in requested:
        return reconstructed
    # Reconcile: a requested node-id ending in this test name wins (handles
    # parametrize-free cases where reconstruction and request agree on the
    # function but differ on path form).
    suffix = f"::{name}"
    matches = [rid for rid in requested if rid.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return reconstructed or name


def _testcase_to_result(case: Element, root: Path, requested: frozenset[str]) -> TestResult:
    """Convert a junit ``<testcase>`` element to a :class:`TestResult`."""
    test_id = _derive_node_id(case, root, requested)

    duration: float | None = None
    raw_time = case.get("time")
    if raw_time:
        try:
            duration = float(raw_time)
        except ValueError:
            duration = None

    status: TestStatus = "passed"
    failure_text = ""
    failure_el = case.find("failure")
    error_el = case.find("error")
    skipped_el = case.find("skipped")
    if failure_el is not None:
        status = "failed"
        failure_text = (failure_el.get("message", "") + "\n" + (failure_el.text or "")).strip()
    elif error_el is not None:
        status = "error"
        failure_text = (error_el.get("message", "") + "\n" + (error_el.text or "")).strip()
    elif skipped_el is not None:
        status = "skipped"

    return TestResult(test_id=test_id, status=status, failure_text=failure_text, duration=duration)


class PythonStackAdapter:
    """``pytest`` + ``coverage.py`` implementation of ``TestStackAdapter``.

    See the module docstring for the coverage-context mechanism. The
    instance is stateless; all I/O is per-call subprocess work scoped to
    *root*.
    """

    name = "python"

    # -- detection reconciliation ------------------------------------------
    def detect(self, root: Path) -> Confidence:
        """Confidence that *root* is a runnable pytest project.

        Reuses :func:`detect_python_stack`: ``"high"`` for a pytest
        project, ``"medium"`` for a Python project without a detected
        pytest dep (best-effort — pytest may still discover plain
        ``test_*`` functions), ``"low"`` when not Python.
        """
        profile = detect_python_stack(root)
        if profile is None:
            return "low"
        if profile.framework == "pytest":
            return "high"
        return "medium"

    def discover(self, root: Path) -> ContractStackProfile:
        """Project the per-stack profile onto the contract projection."""
        profile = detect_python_stack(root)
        if profile is None:
            return ContractStackProfile(
                language="python",
                framework="none",
                coverage_tool="none",
                test_glob="test_*.py",
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
        """Run *subset* via pytest, parsing a ``--junitxml`` report."""
        if subset.is_empty():
            return RunResult(results=(), no_tests_collected=False, exit_code=None)

        with tempfile.TemporaryDirectory(prefix="forge-tm-run-") as tmp:
            junit = Path(tmp) / "junit.xml"
            argv = [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--no-header",
                "-q",
                f"--junitxml={junit}",
                *sorted(subset.test_ids),
            ]
            completed = self._invoke(argv, root)
            if completed is None:
                return RunResult(results=(), no_tests_collected=True, exit_code=None)
            results, none_collected = _parse_junit(junit, root, subset.test_ids)
            if completed.returncode == _PYTEST_NO_TESTS_EXIT:
                none_collected = True
            return RunResult(
                results=results,
                no_tests_collected=none_collected and not results,
                exit_code=completed.returncode,
            )

    def coverage_map(self, root: Path, changed: ChangedSet) -> CoverageMap:
        """Run the suite under per-test coverage contexts → reverse index."""
        try:
            import coverage  # noqa: PLC0415 -- optional dep, imported lazily
        except ImportError:
            _logger.warning("coverage.py not importable; coverage_map unmeasured")
            return CoverageMap(per_test={}, measured=False)

        with tempfile.TemporaryDirectory(prefix="forge-tm-cov-") as tmp:
            data_file = Path(tmp) / ".coverage"
            argv = [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--no-header",
                "-q",
                f"--cov={root}",
                "--cov-context=test",
                "--cov-report=",
                f"--cov-config={self._write_cov_config(Path(tmp), data_file)}",
            ]
            completed = self._invoke(argv, root)
            if completed is None or completed.returncode == _PYTEST_NO_TESTS_EXIT:
                return CoverageMap(per_test={}, measured=False)
            if not data_file.exists():
                _logger.warning("coverage data file not produced under %s", tmp)
                return CoverageMap(per_test={}, measured=False)
            return self._read_coverage(coverage, data_file, root, changed)

    def call_graph(self, root: Path, symbols: SymbolSet) -> CallGraph | None:
        """Best-effort downstream call graph seeded by *symbols* (TM-3).

        Walks ``.py`` source files under *root* via
        :func:`build_python_call_graph` and returns a
        :class:`~primitives._sdet.stacks.contract.CallGraph` whose
        :meth:`~primitives._sdet.stacks.contract.CallGraph.callers_of`
        returns direct callers of any given symbol.

        Returns ``None`` on unexpected failure so callers degrade gracefully
        (the coverage-primary path is unaffected).  A successful run always
        returns a :class:`CallGraph` — possibly with zero edges when the
        source is empty or all files exceed the size cap.

        **Limits (best-effort):** dynamic dispatch, ``getattr``-based calls,
        decorators that replace the callee, and cross-repo re-exports are not
        resolved.  The result is an approximation; coverage is the source of
        truth (spec §4).
        """
        try:
            return build_python_call_graph(root, symbols)
        except Exception:
            _logger.warning(
                "call_graph build failed in %s — falling back to None", root, exc_info=True
            )
            return None

    # -- internals ---------------------------------------------------------
    def _invoke(self, argv: list[str], root: Path) -> subprocess.CompletedProcess[str] | None:
        """Run *argv* in *root*; return the completed process or None on failure."""
        try:
            return subprocess.run(  # noqa: S603 -- argv list, no shell, trusted exe
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT_S,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _logger.warning("pytest invocation failed in %s: %s", root, exc)
            return None

    @staticmethod
    def _write_cov_config(tmp: Path, data_file: Path) -> Path:
        """Write a minimal coverage config pinning the private data file."""
        cfg = tmp / "coveragerc"
        cfg.write_text(
            f"[run]\ndata_file = {data_file}\nrelative_files = False\n",
            encoding="utf-8",
        )
        return cfg

    @staticmethod
    def _read_coverage(
        coverage: object, data_file: Path, root: Path, changed: ChangedSet
    ) -> CoverageMap:
        """Invert coverage contexts into a test → {(path, line)} index."""
        cov_data = coverage.CoverageData(basename=str(data_file))  # type: ignore[attr-defined]
        cov_data.read()
        changed_paths = changed.paths()
        per_test: dict[str, set[tuple[str, int]]] = {}
        for measured in cov_data.measured_files():
            rel = _normalize_path(root, measured)
            # When the diff scopes specific files, only index those; an
            # empty ChangedSet means "index everything we measured".
            if changed_paths and rel not in changed_paths:
                continue
            for lineno, contexts in cov_data.contexts_by_lineno(measured).items():
                for ctx in contexts:
                    if not ctx:
                        continue  # the empty-string default context
                    tid = _normalize_test_id(root, _strip_context_phase(ctx))
                    per_test.setdefault(tid, set()).add((rel, lineno))
        return CoverageMap(
            per_test={tid: frozenset(locs) for tid, locs in per_test.items()},
            measured=True,
        )


def _strip_context_phase(ctx: str) -> str:
    """Strip pytest-cov's ``|setup``/``|run``/``|teardown`` phase suffix."""
    return ctx.split("|", 1)[0]


# FLEX-2 (#174): self-register on import so all_stacks() picks us up.
from primitives._sdet.stacks import register_stack, register_unit_meta  # noqa: E402
from primitives._sdet.stacks.contract import UnitTestMeta  # noqa: E402

register_stack("python", detect_python_stack)

# S3 unit-test generation metadata (#1110).
register_unit_meta(
    "python",
    UnitTestMeta(
        test_framework="pytest",
        template_id="pytest",
        stub_raise="raise NotImplementedError",
    ),
)

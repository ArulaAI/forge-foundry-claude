"""Java test stack detection + execution adapter.

The detection half (``detect_java_stack`` → :class:`StackProfile`)
parses pom.xml (Maven) and build.gradle / build.gradle.kts (Gradle) to
determine: framework (JUnit 5 / JUnit 4 / TestNG / none), Surefire plugin
presence, JaCoCo coverage plugin presence.

The execution half (:class:`JavaStackAdapter`, S2 TM-9 #901) implements
the :class:`~primitives._sdet.stacks.contract.TestStackAdapter` Protocol:
it shells out to ``mvn`` / ``gradle`` to run a subset of tests, parses
Surefire/JUnit XML for pass/fail + failure text, and parses ``jacoco.xml``
for aggregate file→line coverage.

**Coverage mechanism — per-test-CLASS isolation via the cached index
(S2 CP-3, #920).** JaCoCo aggregates coverage over the full test run by
default and does not natively attribute which lines a *specific* test
exercised. But per-test-**class** isolation *is* obtainable by running the
build scoped to one test class at a time with coverage: ``mvn -Dtest=<Class>
test jacoco:report`` (or the Gradle ``test jacocoTestReport
--tests <FQCN>`` equivalent) produces a ``jacoco.xml`` for that one class.
``coverage_map`` therefore drives the cached, incremental
:class:`~primitives._sdet.stacks.coverage_index.CoverageIndex` (CP-1): for
each stale/new test class it runs one scoped build, parses the resulting
``jacoco.xml`` via :func:`_parse_jacoco_xml` (#901, defusedxml) into the
``(source_path, line)`` locations that *one* test class executed, caches it
keyed by content hashes, and materialises a ``measured=True``
:class:`~primitives._sdet.stacks.contract.CoverageMap` (``side="base"``)
from the index. The index is **base-side** (it measures the current/main
tree); a PR consumes it read-only via the base-side line intersection, so an
indirectly-impacted test (one that executes a changed source through a helper
class, without referencing its file) is still selected where static
import-reachability would miss it.

**Cold-start cost.** A scoped Maven/Gradle build is the slowest per-isolation
run of any stack (JVM + plugin startup dwarfs the JS/TS runner): a cold cache
re-measures every test class, so the first build on a fresh checkout is
expensive. The cache is what makes this viable — the index amortises the cost
across PR runs (only stale/new classes are re-measured), and the
``s2.max_coverage_runs`` cap (default 50) bounds the worst case: a cold build
or staleness sweep exceeding the cap degrades to the static fallback
(``measured=False``) rather than running hundreds of scoped builds / hanging
CI. Tooling-missing and parse-failure paths likewise degrade to
``measured=False`` (never fabricating per-test data); the CI overlay's
``actions/cache`` step persists the index across runs (design §9).

**Known limitation — test-class OWN-content staleness is not detected.** The
Java index keys on each test class's FQCN (not a file path), so the staleness
sweep re-measures a class only when a *covered source* it executes changes — not
when the test class's own source changes. Editing a test class without touching
the sources it covers will reuse the cached entry until a covered source also
changes. The primary parity case (a changed covered source re-measuring the
tests that execute it) works normally; only same-file test-only edits are
missed. (JS/TS keys on the test-file path, so it does not share this gap.)

Both ``run_tests`` and ``coverage_map`` are **deterministic + subprocess-
based** — no LLM calls, no network.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from defusedxml.ElementTree import ParseError as DefusedParseError
from defusedxml.ElementTree import parse as defused_parse

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
from primitives._xml_safety import XmlSafetyError, parse_pom_path

if TYPE_CHECKING:
    from pathlib import Path
    from xml.etree.ElementTree import Element

    from primitives._sdet.stacks.contract import CallGraph

_logger = logging.getLogger(__name__)

JavaFramework: TypeAlias = Literal["junit-jupiter", "junit4", "testng", "none"]
JavaCoverage: TypeAlias = Literal["jacoco", "cobertura", "none"]

#: Hard cap on a single Maven/Gradle invocation (seconds).
_RUN_TIMEOUT_S = 600

_MAVEN_NS = "{http://maven.apache.org/POM/4.0.0}"


@dataclass(frozen=True)
class StackProfile:
    """Detected Java test stack profile."""

    language: Literal["java"]
    framework: JavaFramework
    framework_version: str | None
    test_glob: str
    coverage_tool: JavaCoverage
    has_surefire: bool


def _parse_pom_deps_and_plugins(
    pom_path: Path,
) -> tuple[dict[str, str], set[str]]:
    """Parse pom.xml; return (dep_map: groupId:artifactId → version, plugin_artifact_ids)."""
    deps: dict[str, str] = {}
    plugins: set[str] = set()
    try:
        root = parse_pom_path(pom_path)
    except XmlSafetyError as exc:
        _logger.warning("Skipping %s: %s", pom_path, exc)
        return deps, plugins
    if root is None:
        return deps, plugins
    ns = _MAVEN_NS if root.tag.startswith(_MAVEN_NS) else ""

    for dep in root.iter(f"{ns}dependency"):
        gid = dep.find(f"{ns}groupId")
        aid = dep.find(f"{ns}artifactId")
        ver = dep.find(f"{ns}version")
        if gid is not None and aid is not None and gid.text and aid.text:
            key = f"{gid.text.strip()}:{aid.text.strip()}"
            deps[key] = ver.text.strip() if ver is not None and ver.text else ""

    for plugin in root.iter(f"{ns}plugin"):
        aid = plugin.find(f"{ns}artifactId")
        if aid is not None and aid.text:
            plugins.add(aid.text.strip())

    return deps, plugins


def _parse_gradle(gradle_path: Path) -> tuple[set[str], bool, bool]:
    """Return (artifact_set, has_junit_platform, has_jacoco)."""
    artifacts: set[str] = set()
    has_junit_platform = False
    has_jacoco = False
    try:
        content = gradle_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return artifacts, has_junit_platform, has_jacoco

    for match in re.finditer(
        r"""(?:implementation|api|compile|testImplementation|runtimeOnly|compileOnly|testRuntimeOnly)\s*[\(]?\s*['"]([^'"]+)['"]""",
        content,
    ):
        artifacts.add(match.group(1))
    if "useJUnitPlatform" in content:
        has_junit_platform = True
    if re.search(r"apply\s+plugin:\s*['\"]jacoco['\"]|id\s+['\"]jacoco['\"]", content):
        has_jacoco = True
    return artifacts, has_junit_platform, has_jacoco


_MIN_GA_PARTS = 2
_MIN_GAV_PARTS = 3


def _framework_from_pom(deps: dict[str, str]) -> tuple[JavaFramework, str | None]:
    if "org.junit.jupiter:junit-jupiter" in deps:
        return "junit-jupiter", deps["org.junit.jupiter:junit-jupiter"] or None
    for key, ver in deps.items():
        if key.startswith("org.junit.jupiter:"):
            return "junit-jupiter", ver or None
    if "junit:junit" in deps:
        return "junit4", deps["junit:junit"] or None
    if "org.testng:testng" in deps:
        return "testng", deps["org.testng:testng"] or None
    return "none", None


def _framework_from_gradle(
    artifacts: set[str], has_junit_platform: bool
) -> tuple[JavaFramework, str | None]:
    for artifact in artifacts:
        parts = artifact.split(":")
        if len(parts) >= _MIN_GA_PARTS:
            ga = f"{parts[0]}:{parts[1]}"
            ver = parts[2] if len(parts) >= _MIN_GAV_PARTS else None
            if ga.startswith("org.junit.jupiter:"):
                return "junit-jupiter", ver
            if ga == "junit:junit":
                return "junit4", ver
            if ga == "org.testng:testng":
                return "testng", ver
    if has_junit_platform:
        return "junit-jupiter", None
    return "none", None


def detect_java_stack(project_root: Path) -> StackProfile | None:
    """Detect Java test stack in *project_root*; return None if not a Java project."""
    pom = project_root / "pom.xml"
    gradle = project_root / "build.gradle"
    gradle_kts = project_root / "build.gradle.kts"

    if not (pom.is_file() or gradle.is_file() or gradle_kts.is_file()):
        return None

    framework: JavaFramework = "none"
    framework_version: str | None = None
    coverage_tool: JavaCoverage = "none"
    has_surefire = False

    if pom.is_file():
        deps, plugins = _parse_pom_deps_and_plugins(pom)
        framework, framework_version = _framework_from_pom(deps)
        has_surefire = "maven-surefire-plugin" in plugins
        if "jacoco-maven-plugin" in plugins:
            coverage_tool = "jacoco"
        elif "cobertura-maven-plugin" in plugins:
            coverage_tool = "cobertura"

    gradle_file = gradle if gradle.is_file() else (gradle_kts if gradle_kts.is_file() else None)
    if gradle_file is not None:
        artifacts, has_junit_platform, has_jacoco = _parse_gradle(gradle_file)
        if framework == "none":
            framework, framework_version = _framework_from_gradle(artifacts, has_junit_platform)
        if has_jacoco and coverage_tool == "none":
            coverage_tool = "jacoco"

    return StackProfile(
        language="java",
        framework=framework,
        framework_version=framework_version,
        test_glob="*Test.java",
        coverage_tool=coverage_tool,
        has_surefire=has_surefire,
    )


# ---------------------------------------------------------------------------
# Execution adapter (S2 TM-9, #901)
# ---------------------------------------------------------------------------


def _parse_surefire_xml(
    xml_path: Path,
) -> tuple[tuple[TestResult, ...], bool]:
    """Parse a Surefire (or JUnit) XML report → (results, no_tests_collected).

    Handles both the ``<testsuite>`` root form (single-suite Surefire, where
    the root element itself is ``<testsuite>``) and the ``<testsuites>``
    wrapper form (Gradle / aggregated reports, where ``<testsuite>`` elements
    are children of ``<testsuites>``). Test ids are
    ``<classname>.<testname>`` — the canonical Surefire form.
    """
    try:
        tree = defused_parse(xml_path)
    except (DefusedParseError, OSError):
        return (), True

    root = tree.getroot()
    if root is None:
        return (), True
    results: list[TestResult] = []

    # When the document root is <testsuite>, parse it directly; otherwise
    # (root is <testsuites>) iterate the nested <testsuite> elements.
    if root.tag == "testsuite":
        for case in root.iterfind("testcase"):
            results.append(_surefire_testcase_to_result(case))
    else:
        for suite in root.iterfind(".//testsuite"):
            for case in suite.iterfind("testcase"):
                results.append(_surefire_testcase_to_result(case))

    return tuple(results), not results


def _surefire_testcase_to_result(case: Element) -> TestResult:
    """Convert a Surefire ``<testcase>`` element to a :class:`TestResult`."""
    classname = case.get("classname", "")
    name = case.get("name", "")
    test_id = f"{classname}.{name}" if classname else name

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


def _parse_surefire_dir(reports_dir: Path) -> tuple[tuple[TestResult, ...], bool]:
    """Parse all ``TEST-*.xml`` files under *reports_dir* → (results, no_tests_collected)."""
    all_results: list[TestResult] = []
    any_file = False
    for xml_file in sorted(reports_dir.glob("TEST-*.xml")):
        any_file = True
        results, _ = _parse_surefire_xml(xml_file)
        all_results.extend(results)
    return tuple(all_results), not (any_file and all_results)


def _parse_jacoco_xml(jacoco_path: Path) -> dict[str, frozenset[int]]:
    """Parse ``jacoco.xml`` → dict of POSIX source path → covered line numbers.

    JaCoCo's ``<sourcefile name="Foo.java">`` elements live under
    ``<package name="com/example">``; we reconstruct the POSIX source path as
    ``com/example/Foo.java`` (the form that matches standard Maven
    ``src/main/java/`` layouts when prefixed). Lines with ``ci > 0``
    (covered instructions) are included; lines with ``ci = 0`` are missed.

    Returns an empty dict when the file is absent or malformed.
    """
    if not jacoco_path.is_file():
        return {}

    try:
        tree = defused_parse(jacoco_path)
    except (DefusedParseError, OSError):
        _logger.warning("jacoco.xml parse failed: %s", jacoco_path)
        return {}

    coverage: dict[str, frozenset[int]] = {}
    root = tree.getroot()
    if root is None:
        return coverage
    for pkg in root.iterfind(".//package"):
        pkg_name = pkg.get("name", "")
        for src in pkg.iterfind("sourcefile"):
            src_name = src.get("name", "")
            if not src_name:
                continue
            path = f"{pkg_name}/{src_name}" if pkg_name else src_name
            covered_lines: set[int] = set()
            for line_el in src.iterfind("line"):
                nr_str = line_el.get("nr")
                ci_str = line_el.get("ci", "0")
                if nr_str is None:
                    continue
                try:
                    nr = int(nr_str)
                    ci = int(ci_str)
                except ValueError:
                    continue
                if ci > 0:
                    covered_lines.add(nr)
            if covered_lines:
                coverage[path] = frozenset(covered_lines)
    return coverage


# ---------------------------------------------------------------------------
# Per-test-CLASS isolation measurer (S2 CP-3, #920) — the Java ``MeasureFn``
# ---------------------------------------------------------------------------

JavaBuildTool: TypeAlias = Literal["maven", "gradle"]

#: Directories never scanned for test classes (build output / vcs).
_SKIP_DIRS: frozenset[str] = frozenset({"target", "build", ".git", ".gradle", "out", "bin"})

#: Filename suffixes the JUnit/Surefire default ``*Test`` / ``*Tests`` /
#: ``Test*`` conventions match — the set scanned to seed the index's test-class
#: id set under ``src/test/java`` trees.
_TEST_CLASS_RE = re.compile(r"^(?:Test.+|.+Tests?|.+TestCase|.+IT)\.java$")

#: Hard cap on a single scoped Maven/Gradle isolation build (seconds). A
#: scoped build is far slower than a JS/TS runner; the cap keeps a wedged
#: build from hanging the staleness sweep.
_ISOLATION_TIMEOUT_S = 600


def detect_build_tool(root: Path) -> JavaBuildTool | None:
    """Return the build tool driving *root* (Maven preferred), or ``None``.

    Maven wins when ``pom.xml`` is present (it is the conventional default and
    its ``-Dtest=`` selection is the most portable); otherwise Gradle when a
    ``build.gradle`` / ``build.gradle.kts`` is present. ``None`` when neither
    build file exists.
    """
    if (root / "pom.xml").is_file():
        return "maven"
    if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        return "gradle"
    return None


def _fqcn_for(java_file: Path, root: Path) -> str | None:
    """Derive a test class's fully-qualified name from its ``src/test/java`` path.

    ``<root>/src/test/java/com/example/CalcTest.java`` →
    ``com.example.CalcTest``. The FQCN is rebuilt from the path segments *after*
    the ``src/test/java`` (or ``src/test/kotlin``) marker so it is independent
    of the ``package`` declaration's formatting — and it matches the
    ``<classname>`` prefix of :class:`RunResult` ids
    (``com.example.CalcTest.testAdd``), so the index keys line up with
    ``run_tests``. Returns ``None`` when the file is not under a recognised
    test-source root.
    """
    try:
        rel_parts = java_file.relative_to(root).parts
    except ValueError:
        return None
    # Find the ``src/<...>/test/<lang>`` marker; FQCN is the tail after it.
    for i in range(len(rel_parts) - 1):
        if (
            rel_parts[i] == "src"
            and i + 2 < len(rel_parts)
            and rel_parts[i + 2] in ("java", "kotlin")
            and rel_parts[i + 1] == "test"
        ):
            pkg_parts = rel_parts[i + 3 : -1]
            class_name = rel_parts[-1][: -len(".java")]
            return ".".join((*pkg_parts, class_name))
    return None


def discover_test_classes(root: Path) -> list[str]:
    """Return the FQCNs of every JUnit-style test class under *root*.

    Scans ``src/test/java`` (and ``src/test/kotlin``) trees for files matching
    the conventional ``*Test`` / ``*Tests`` / ``Test*`` / ``*IT`` naming, skips
    vendor/build dirs and symlinked files (defense in depth, mirroring the
    emitter's symlink discipline), and maps each to its FQCN via
    :func:`_fqcn_for`. Deterministically sorted so the index's id set is stable
    across machines. These FQCNs are the per-test-**class** keys the coverage
    index is built on, matching the ``<classname>`` part of
    :func:`_surefire_testcase_to_result`'s run ids.
    """
    seen: set[str] = set()
    for java_file in root.rglob("*.java"):
        if not java_file.is_file() or java_file.is_symlink():
            continue
        if any(part in _SKIP_DIRS for part in java_file.parts):
            continue
        if not _TEST_CLASS_RE.match(java_file.name):
            continue
        fqcn = _fqcn_for(java_file, root)
        if fqcn is not None:
            seen.add(fqcn)
    return sorted(seen)


def _build_isolation_argv(
    runner: str,
    build_tool: JavaBuildTool,
    fqcn: str,
) -> list[str]:
    """Build the argv for a coverage build scoped to one test class *fqcn*.

    - **Maven:** ``mvn -Dtest=<Simple> test jacoco:report`` (Maven's
      ``-Dtest`` accepts the simple class name; the JaCoCo report goes to the
      conventional ``target/site/jacoco/jacoco.xml``).
    - **Gradle:** ``<gradle> test jacocoTestReport --tests <FQCN>``
      (Gradle's ``--tests`` takes the fully-qualified name; the report goes to
      ``build/reports/jacoco/test/jacocoTestReport.xml``).

    List argv, no shell. The simple class name (Maven) is the FQCN's last
    segment.
    """
    if build_tool == "gradle":
        return [runner, "test", "jacocoTestReport", "--tests", fqcn, "--console=plain"]
    simple = fqcn.rsplit(".", 1)[-1]
    return [runner, f"-Dtest={simple}", "test", "jacoco:report", "-q", "--batch-mode"]


def _resolve_isolation_runner(root: Path, build_tool: JavaBuildTool) -> str | None:
    """Resolve the build runner binary for a scoped isolation build, or ``None``.

    Maven uses the ``mvn`` on PATH; Gradle prefers the project ``./gradlew``
    wrapper, falling back to a PATH ``gradle``. ``None`` when nothing resolves
    (the measurer then degrades to UNMEASURED).
    """
    if build_tool == "maven":
        return shutil.which("mvn")
    gradlew = root / "gradlew"
    return str(gradlew) if gradlew.is_file() else shutil.which("gradle")


@dataclass(frozen=True)
class JavaCoverageMeasurer:
    """A :class:`~primitives._sdet.stacks.coverage_index.MeasureFn` for Java.

    Given the repo *root* and a test-class id (FQCN, e.g.
    ``com.example.CalcTest``), runs the detected build tool scoped to that
    **one** test class with coverage (``mvn -Dtest=<Class> test jacoco:report``
    or the Gradle equivalent), parses the resulting ``jacoco.xml`` via
    :func:`_parse_jacoco_xml`, and returns the ``(source_path, line)`` locations
    that test class executed (per-test-**class** granularity).

    Deterministic + subprocess-safe (list argv, no ``shell=True``). Robust to a
    missing build tool, a failed build, and absent/malformed coverage output:
    returns an **empty frozenset** (UNMEASURED for that class) rather than
    fabricating attribution. The index layer (CP-1) caches the result keyed by
    content hashes, amortising the slow scoped builds.
    """

    build_tool: JavaBuildTool

    def __call__(self, root: Path, test_id: str) -> CoveredLocations:
        """Measure covered ``(source_path, line)`` locations for one test class."""
        runner = _resolve_isolation_runner(root, self.build_tool)
        if runner is None:
            _logger.warning(
                "Java build runner for %s not found; %s unmeasured", self.build_tool, test_id
            )
            return frozenset()

        argv = _build_isolation_argv(runner, self.build_tool, test_id)
        completed = self._invoke(argv, root)
        if completed is None:
            return frozenset()

        jacoco_path = JavaStackAdapter._find_jacoco_xml(root)
        if jacoco_path is None:
            _logger.warning("jacoco.xml not produced for %s under %s", test_id, root)
            return frozenset()
        return _jacoco_to_locations(jacoco_path)

    @staticmethod
    def _invoke(argv: list[str], root: Path) -> subprocess.CompletedProcess[str] | None:
        """Run *argv* in *root*; return the completed process or None on failure."""
        try:
            return subprocess.run(  # noqa: S603 -- argv list, no shell, trusted exe
                argv,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=_ISOLATION_TIMEOUT_S,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _logger.warning("Java isolation build failed in %s: %s", root, exc)
            return None


def _jacoco_to_locations(jacoco_path: Path) -> CoveredLocations:
    """Flatten a parsed ``jacoco.xml`` into a ``(source_path, line)`` frozenset.

    Reuses :func:`_parse_jacoco_xml` (#901, defusedxml) — ``{path: {lines}}`` —
    and explodes it to the flat covered-location set the coverage index keys
    on. An absent/malformed file yields an empty frozenset (the parser already
    degrades), which the caller treats as UNMEASURED.
    """
    coverage = _parse_jacoco_xml(jacoco_path)
    return frozenset((path, line) for path, lines in coverage.items() for line in lines)


class JavaStackAdapter:
    """``mvn``/``gradle`` + Surefire/JaCoCo implementation of ``TestStackAdapter``.

    See the module docstring for the coverage mechanism: per-test-**class**
    JaCoCo isolation builds feed the cached ``CoverageIndex``, so ``coverage_map``
    returns ``measured=True`` (``side="base"``) in the common case and degrades
    to the engine's TM-2 static fallback (``measured=False``) only when the build
    tool is missing, the staleness sweep exceeds the cap, or no isolation build
    produced parseable coverage (S2 CP-3, #920). The instance is stateless; all
    I/O is per-call subprocess work scoped to *root*.
    """

    name = "java"

    # -- detection reconciliation ------------------------------------------
    def detect(self, root: Path) -> Confidence:
        """Confidence that *root* is a runnable Java test project.

        Reuses :func:`detect_java_stack`: ``"high"`` when a JUnit framework
        is detected, ``"medium"`` when the project has build files but no
        recognised framework, ``"low"`` when not a Java project.
        """
        profile = detect_java_stack(root)
        if profile is None:
            return "low"
        if profile.framework != "none":
            return "high"
        return "medium"

    def discover(self, root: Path) -> ContractStackProfile:
        """Project the per-stack profile onto the contract projection."""
        profile = detect_java_stack(root)
        if profile is None:
            return ContractStackProfile(
                language="java",
                framework="none",
                coverage_tool="none",
                test_glob="*Test.java",
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
        """Run *subset* via ``mvn test`` / ``gradle test``, parsing Surefire XML.

        For Maven projects the Surefire plugin writes XML reports to
        ``target/surefire-reports/TEST-*.xml``; for Gradle they land under
        ``build/test-results/test/TEST-*.xml``. The subset is currently
        applied as a post-filter over the reports (running the whole suite
        then filtering) rather than via ``-Dtest=...`` method-level
        selection, which would require encoding the class.method form across
        Maven + Gradle consistently. **This means CI cost scales with the
        full suite** — acceptable for now; a ``-Dtest=`` optimisation is
        deferred.

        An empty *subset* returns an empty :class:`RunResult` without
        invoking any runner.
        """
        if subset.is_empty():
            return RunResult(results=(), no_tests_collected=False, exit_code=None)

        with tempfile.TemporaryDirectory(prefix="forge-tm-java-run-") as _tmp:
            completed = self._invoke_runner(root)
            if completed is None:
                return RunResult(results=(), no_tests_collected=True, exit_code=None)

            all_results, none_collected = self._collect_results(root)

            # Filter to the requested subset (by test_id match).
            filtered = tuple(r for r in all_results if r.test_id in subset.test_ids)
            if not filtered and not none_collected:
                none_collected = True

            return RunResult(
                results=filtered,
                no_tests_collected=none_collected and not filtered,
                exit_code=completed.returncode,
            )

    def coverage_map(self, root: Path, changed: ChangedSet) -> CoverageMap:
        """Per-test-class coverage via the cached CP-1 index → ``measured=True``.

        Drives the incremental :class:`CoverageIndex` (CP-1) with per-test-CLASS
        isolation builds (S2 CP-3, #920):

        1. Load the cached base index (``default_index_path`` / ``load_index``);
           an absent cache is a cold start, not an error.
        2. Discover the project's test classes and ``refresh`` the stale/new
           ones via :class:`JavaCoverageMeasurer` (one ``mvn -Dtest=<Class> test
           jacoco:report`` — or Gradle ``--tests <FQCN>`` — build each), bounded
           by ``s2.max_coverage_runs``. A :class:`CapExceededError` (over-cap) →
           ``measured=False`` (static fallback), measuring nothing — Java's
           scoped builds are the slowest per run, so the cap matters most here.
        3. Persist the refreshed index (``save_index``) so later PR runs reuse it.
        4. ``materialize_coverage_map(index, changed)`` → a ``measured=True``
           :class:`CoverageMap` tagged ``side="base"`` (the index is base-side).

        Honest degradation to ``measured=False`` (the engine's TM-2 static
        fallback) when: not a Maven/Gradle project, the build tool is missing,
        the staleness sweep exceeds the cap, or no isolation build produced
        parseable coverage. Per-test attribution is never fabricated. ``changed``
        is consumed by ``materialize_coverage_map`` / the impact engine's
        base-side intersection — not here.
        """
        build_tool = detect_build_tool(root)
        if build_tool is None:
            _logger.info("no Maven/Gradle build file under %s; coverage_map unmeasured", root)
            return CoverageMap(per_test={}, measured=False)

        if _resolve_isolation_runner(root, build_tool) is None:
            _logger.warning(
                "Java build runner for %s not found; coverage_map unmeasured", build_tool
            )
            return CoverageMap(per_test={}, measured=False)

        index_path = default_index_path(root, self.name)
        index = load_index(index_path, stack=self.name)
        test_classes = discover_test_classes(root)
        measurer = JavaCoverageMeasurer(build_tool=build_tool)
        max_runs = SdetConfig.from_project(root).s2.max_coverage_runs

        try:
            index = refresh(
                index,
                root,
                test_files=test_classes,
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
        """Best-effort downstream call graph — not yet implemented (TM-3).

        Java's static typing makes a downstream call graph more feasible
        than in dynamic languages; however building it requires either a
        bytecode analyser (ASM, BCEL) or a source-level parser (JavaParser),
        neither of which is in the current dependency set. Deferred to TM-3.
        """
        return None

    # -- internals ---------------------------------------------------------
    def _invoke_runner(self, root: Path) -> subprocess.CompletedProcess[str] | None:
        """Invoke ``mvn test`` or ``gradle test`` in *root*.

        Prefers Maven when ``pom.xml`` is present; falls back to Gradle.
        Returns the completed process or ``None`` when no runner is available
        or the invocation fails with a non-subprocess error.
        """
        pom = root / "pom.xml"
        if pom.is_file():
            mvn = shutil.which("mvn")
            if mvn:
                argv = [mvn, "test", "-q", "--batch-mode"]
                return self._run(argv, root)
            _logger.warning("mvn not found on PATH; skipping Maven run in %s", root)

        gradle = root / "build.gradle"
        gradle_kts = root / "build.gradle.kts"
        if gradle.is_file() or gradle_kts.is_file():
            gradlew = root / "gradlew"
            runner = str(gradlew) if gradlew.is_file() else shutil.which("gradle")
            if runner:
                argv = [runner, "test", "--console=plain"]
                return self._run(argv, root)
            _logger.warning("gradle not found on PATH; skipping Gradle run in %s", root)

        _logger.warning("No Java test runner available in %s", root)
        return None

    def _run(self, argv: list[str], root: Path) -> subprocess.CompletedProcess[str] | None:
        """Execute *argv* in *root*; return the completed process or None."""
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
            _logger.warning("Java runner invocation failed in %s: %s", root, exc)
            return None

    @staticmethod
    def _collect_results(root: Path) -> tuple[tuple[TestResult, ...], bool]:
        """Gather parsed results from all known report directories."""
        # Maven Surefire
        maven_reports = root / "target" / "surefire-reports"
        if maven_reports.is_dir():
            return _parse_surefire_dir(maven_reports)

        # Gradle default test result location
        gradle_reports = root / "build" / "test-results" / "test"
        if gradle_reports.is_dir():
            return _parse_surefire_dir(gradle_reports)

        return (), True

    @staticmethod
    def _find_jacoco_xml(root: Path) -> Path | None:
        """Locate ``jacoco.xml`` under standard Maven and Gradle output paths."""
        candidates = [
            root / "target" / "site" / "jacoco" / "jacoco.xml",
            root / "build" / "reports" / "jacoco" / "test" / "jacocoTestReport.xml",
        ]
        for path in candidates:
            if path.is_file():
                return path
        return None


# FLEX-2 (#174): self-register on import so all_stacks() picks us up.
from primitives._sdet.stacks import register_stack, register_unit_meta  # noqa: E402
from primitives._sdet.stacks.contract import UnitTestMeta  # noqa: E402

register_stack("java", detect_java_stack)

# S3 unit-test generation metadata (#1110).
register_unit_meta(
    "java",
    UnitTestMeta(
        test_framework="junit5",
        template_id="junit5",
        stub_raise='throw new UnsupportedOperationException("not implemented");',
    ),
)

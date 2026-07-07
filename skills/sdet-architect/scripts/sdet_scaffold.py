#!/usr/bin/env python3
"""Deterministic stack-detection, config scaffolding, and doc assembly for ``/sdet-architect``.

Token-offload: the model supplies its per-layer tooling picks + ADR rationale prose as a compact
JSON payload.  This stdlib-only script does the deterministic remainder:

* **Stack detection** — scan the project root for Python (``pyproject.toml`` / ``requirements.txt``
  / ``tests/`` dir), JS/TS (``package.json`` deps/scripts), and Java (``pom.xml`` /
  ``build.gradle``/``build.gradle.kts``).
* **Idempotent config scaffolding** — write ``pytest.ini`` for Python (skip if ``pytest.ini``
  exists or ``pyproject.toml`` already has ``[tool.pytest.ini_options]``); write
  ``vitest.config.ts`` for JS/TS when no vitest/jest config exists; patch ``package.json``
  ``scripts.test`` when absent; inject Surefire into ``pom.xml`` / append ``useJUnitPlatform()``
  to ``build.gradle`` for Java.  All writes are idempotent — no overwrite when the config block is
  already present.
* **Fixed 8-section ``test-architecture.md`` assembly** — render the 8 canonical sections in
  their exact order from the structured model payload (Stack profile, Test cone target, Layer 1 —
  Unit, Layer 2 — Contract, Layer 3 — Integration, Layer 4 — E2E, Test data strategy, CI
  integration), each section ending with its Rationale ADR block whose prose comes from the model.
* **Scoped-analysis mode (``--scoped-findings``)** — validate and write a model-supplied
  ``s1-findings.json`` (conventions + gaps) to a run directory, for consumption by S2
  ``test_maintainer``.  The model infers conventions and gaps from the impacted area; this
  script validates structural integrity and writes the artifact under ``--run-dir``.

The model keeps: per-layer tooling picks (Pact vs JSON Schema, Testcontainers vs WireMock, etc.)
and the Rationale ADR prose.  It emits a structured JSON payload; this script detects stacks,
scaffolds configs, and renders the doc.

Runnable with a bare ``python3`` (stdlib only — no installed foundry CLI dependency needed), which
is why the skill body can invoke it in any harness session.

Usage::

    python3 sdet_scaffold.py --payload payload.json --project-root ./myproject

    python3 sdet_scaffold.py --payload - --project-root . < payload.json

    python3 sdet_scaffold.py --detect-only --project-root ./myproject

    python3 sdet_scaffold.py --scoped-findings findings.json --run-dir /tmp/run-xyz
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

#: Characters disallowed in user-supplied filename components.
_UNSAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _safe_filename(value: str) -> str:
    """Collapse anything outside ``[A-Za-z0-9._-]`` to ``_`` so a payload-supplied
    name (e.g. a project name) cannot inject path separators into the output path."""
    return _UNSAFE_FILENAME_RE.sub("_", value) or "project"


def _safe_output_path(project_root: Path, relative: str) -> Path:
    """Resolve *relative* under *project_root* and reject any path-traversal attempt.

    Raises ``ValueError`` when the resolved path escapes the project root (``..`` in
    the raw string, absolute path, or resolved path outside root).
    """
    if ".." in relative or relative.startswith("/"):
        raise ValueError(f"path traversal rejected: {relative!r}")
    resolved = (project_root / relative).resolve()
    root_resolved = project_root.resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError(f"output path {resolved!r} escapes project root {root_resolved!r}")
    return resolved


# ---------------------------------------------------------------------------
# Stack detection
# ---------------------------------------------------------------------------

KNOWN_STACKS: tuple[str, ...] = ("python", "js_ts", "java")


@dataclass(frozen=True)
class StackProfile:
    """Detected stack information for one language."""

    language: str  # one of KNOWN_STACKS
    manifest_files: list[str]  # e.g. ["pyproject.toml", "tests/"]
    has_tests_dir: bool = False
    root: str = "."  # POSIX relative path from the scanned project root ("." = repo root)


def detect_python(project_root: Path) -> StackProfile | None:
    """Detect a Python stack: pyproject.toml OR requirements.txt, plus optional tests/."""
    root = project_root.resolve()
    manifests: list[str] = []
    has_toml = (root / "pyproject.toml").is_file()
    has_req = (root / "requirements.txt").is_file()
    has_tests = (root / "tests").is_dir()
    if has_toml:
        manifests.append("pyproject.toml")
    if has_req:
        manifests.append("requirements.txt")
    if has_tests:
        manifests.append("tests/")
    if not (has_toml or has_req):
        return None
    return StackProfile(language="python", manifest_files=manifests, has_tests_dir=has_tests)


def detect_js_ts(project_root: Path) -> StackProfile | None:
    """Detect a JS/TS stack: package.json present."""
    root = project_root.resolve()
    pkg = root / "package.json"
    if not pkg.is_file():
        return None
    return StackProfile(language="js_ts", manifest_files=["package.json"])


def detect_java(project_root: Path) -> StackProfile | None:
    """Detect a Java stack: pom.xml OR build.gradle / build.gradle.kts present."""
    root = project_root.resolve()
    manifests: list[str] = []
    if (root / "pom.xml").is_file():
        manifests.append("pom.xml")
    if (root / "build.gradle").is_file():
        manifests.append("build.gradle")
    if (root / "build.gradle.kts").is_file():
        manifests.append("build.gradle.kts")
    if not manifests:
        return None
    return StackProfile(language="java", manifest_files=manifests)


#: Directory names never descended into during monorepo sub-stack detection
#: (dependency / build / VCS trees). Hidden directories (``.git``, ``.venv``, the
#: emitted ``.foundry`` / ``.claude`` / ``.cursor`` trees, …) are skipped wholesale
#: by the leading-dot rule in :func:`_iter_scan_dirs`, so only non-dot noise dirs
#: need listing here.
_SKIP_SCAN_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "bower_components",
        "vendor",
        "venv",
        "env",
        "__pycache__",
        "site-packages",
        "dist",
        "build",
        "target",
        "out",
        "coverage",
    }
)

#: Maximum directory depth descended when scanning for monorepo sub-stacks.
#: 0 = project root only; 2 catches the common ``apps/<x>/`` and ``packages/<x>/``
#: monorepo layouts without walking the entire tree.
_MAX_SCAN_DEPTH = 2


def _iter_scan_dirs(project_root: Path, max_depth: int = _MAX_SCAN_DEPTH) -> list[Path]:
    """Return *project_root* plus each non-excluded subdirectory up to *max_depth*.

    Bounded breadth-first walk that skips dependency/build/VCS dirs, hidden dirs,
    and symlinked dirs (symlink-safety, mirroring SEC-10). Used so monorepo
    sub-stacks (e.g. a JS/TS ``portal/`` beside a Python repo root) are detected,
    not only the manifests sitting at the repo root.
    """
    root = project_root.resolve()
    dirs: list[Path] = [root]
    frontier: list[tuple[Path, int]] = [(root, 0)]
    while frontier:
        current, depth = frontier.pop()
        if depth >= max_depth:
            continue
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for child in entries:
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith(".") or child.name in _SKIP_SCAN_DIRS:
                continue
            dirs.append(child)
            frontier.append((child, depth + 1))
    return dirs


#: Closed set of data backends DT-1 designs data-testing strategies for (#1134).
#:
#: GUARDED MIRROR of the canonical ``_sdet`` data-backend registry
#: (``primitives/_sdet/stacks/data_backends.py``). This script is emitted
#: standalone into a consumer's skill tree and runs WITHOUT the foundry Python
#: package on the path, so it cannot ``import primitives._sdet`` at runtime —
#: the constants below are an intentional self-contained copy. They are kept
#: byte-identical to the registry by ``tests/test_sdet_data_backend_mirror.py``,
#: which fails CI on any drift (DT-2 / #1124, #1157). The registry is the source
#: of truth; the in-package consumer S4 ``test_scaffold`` (PLB) imports it directly.
DATA_BACKENDS: tuple[str, ...] = ("postgres", "mysql", "mssql", "oracle", "dynamodb", "mongodb")

#: Lowercased driver substrings per backend, matched against manifest text
#: (py / node / java / .NET drivers). Guarded mirror of the ``_sdet`` registry's
#: per-backend ``signals`` (see the note on :data:`DATA_BACKENDS`).
_DATA_BACKEND_SIGNALS: dict[str, tuple[str, ...]] = {
    "postgres": ("psycopg", "asyncpg", "org.postgresql", "npgsql", '"pg"'),
    "mysql": ("pymysql", "mysqlclient", "mysql-connector", "mysql2", "mysqlconnector"),
    "mssql": ("pyodbc", "pymssql", "mssql-jdbc", "microsoft.data.sqlclient", "tedious"),
    "oracle": ("oracledb", "cx_oracle", "ojdbc", "oracle.manageddataaccess"),
    "dynamodb": ("boto3", "aioboto3", "client-dynamodb", "awssdk.dynamodbv2"),
    "mongodb": ("pymongo", "motor", "mongoose", "mongodb-driver", "mongodb.driver"),
}

#: Dependency-manifest globs scanned for data-backend driver signals. Guarded
#: mirror of the ``_sdet`` registry's ``_DATA_MANIFEST_GLOBS`` (see
#: :data:`DATA_BACKENDS`).
_DATA_MANIFEST_GLOBS: tuple[str, ...] = (
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "*.csproj",
)


def detect_data_backends(project_root: Path) -> list[str]:
    """Detect data backends from dependency manifests (#1134); sorted closed-set names.

    Scans the project root plus bounded sub-projects (same walk as
    :func:`detect_stacks`), reads each dependency manifest, and matches the
    per-backend driver signals. Returns a sorted list drawn from
    :data:`DATA_BACKENDS` (empty when none found). Self-contained guarded mirror
    of ``_sdet.detect_data_backends`` (see :data:`DATA_BACKENDS` for why this
    script can't import the registry directly).
    """
    found: set[str] = set()
    for scan_dir in _iter_scan_dirs(project_root.resolve()):
        for pattern in _DATA_MANIFEST_GLOBS:
            for manifest in sorted(scan_dir.glob(pattern)):
                if manifest.is_symlink() or not manifest.is_file():
                    continue
                try:
                    text = manifest.read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:
                    continue
                for backend, signals in _DATA_BACKEND_SIGNALS.items():
                    if any(sig in text for sig in signals):
                        found.add(backend)
    return sorted(found)


def parse_data_backend_arg(value: str) -> list[str]:
    """Parse + validate a ``--data-backend`` override (comma-separated, closed set).

    Rejects any name not in :data:`DATA_BACKENDS` (raises :class:`ValueError`), so a
    typo or an attacker-controlled value can never widen the closed set.
    """
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    bad = [n for n in names if n not in DATA_BACKENDS]
    if bad:
        raise ValueError(
            f"unknown --data-backend value(s) {bad!r}; valid: {', '.join(DATA_BACKENDS)}"
        )
    # Preserve closed-set order, de-duplicated.
    return [b for b in DATA_BACKENDS if b in set(names)]


def resolve_data_backends(project_root: Path, override: str | None) -> list[str]:
    """The data backends to design for: the validated ``--data-backend`` override if
    given, else inline detection (#1134)."""
    if override:
        return parse_data_backend_arg(override)
    return detect_data_backends(project_root)


def detect_stacks(project_root: Path) -> list[StackProfile]:
    """Return every detected stack in *project_root* and its sub-projects.

    Scans the project root plus bounded, non-excluded subdirectories so monorepo
    layouts (a JS/TS ``portal/`` next to a Python repo root) are detected, not only
    the manifests at the repo root. Each profile records the ``root`` (POSIX
    relative path) where it was found so the caller can scaffold and render per
    sub-project. Deduplicated by ``(language, root)``; may be empty.
    """
    base = project_root.resolve()
    results: list[StackProfile] = []
    seen: set[tuple[str, str]] = set()
    for scan_dir in _iter_scan_dirs(base):
        rel = "." if scan_dir == base else scan_dir.relative_to(base).as_posix()
        for detector in (detect_python, detect_js_ts, detect_java):
            profile = detector(scan_dir)
            if profile is None:
                continue
            key = (profile.language, rel)
            if key in seen:
                continue
            seen.add(key)
            results.append(replace(profile, root=rel))
    return results


# ---------------------------------------------------------------------------
# Idempotent config scaffolding
# ---------------------------------------------------------------------------

_PYTEST_INI = """\
[pytest]
testpaths = tests
addopts = -v --tb=short
"""

_VITEST_CONFIG_TS = """\
import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    globals: true,
    environment: 'node',
    include: ['**/*.{test,spec}.{ts,tsx,js,jsx}'],
  },
})
"""

_SUREFIRE_SNIPPET = """\
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>3.2.5</version>
      </plugin>"""

_GRADLE_JUNIT_PLATFORM = "\ntest {\n    useJUnitPlatform()\n}\n"


@dataclass
class ScaffoldResult:
    """What the scaffolder wrote or skipped."""

    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def scaffold_python(project_root: Path, result: ScaffoldResult) -> None:
    """Write ``pytest.ini`` if no pytest config block exists."""
    root = project_root.resolve()
    ini = root / "pytest.ini"
    if ini.is_file():
        result.skipped.append("pytest.ini (already exists)")
        return
    toml = root / "pyproject.toml"
    if toml.is_file() and "[tool.pytest.ini_options]" in toml.read_text(encoding="utf-8"):
        result.skipped.append("pytest.ini ([tool.pytest.ini_options] in pyproject.toml)")
        return
    ini.write_text(_PYTEST_INI, encoding="utf-8")
    result.written.append("pytest.ini")


def scaffold_js_ts(project_root: Path, result: ScaffoldResult) -> None:
    """Write ``vitest.config.ts`` and/or patch ``package.json`` ``scripts.test``."""
    root = project_root.resolve()

    # 1. vitest/jest config — the two are gated independently
    vitest_configs = list(root.glob("vitest.config.*")) + list(root.glob("jest.config.*"))
    if vitest_configs:
        result.skipped.append(f"vitest.config.ts ({vitest_configs[0].name} already exists)")
    else:
        vconf = root / "vitest.config.ts"
        vconf.write_text(_VITEST_CONFIG_TS, encoding="utf-8")
        result.written.append("vitest.config.ts")

    # 2. package.json scripts.test — independent gate
    pkg = root / "package.json"
    if not pkg.is_file():
        return
    try:
        data: dict[str, object] = json.loads(pkg.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        result.skipped.append("package.json scripts.test (JSON parse error)")
        return
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
        data["scripts"] = scripts
    if "test" in scripts:
        result.skipped.append("package.json scripts.test (already set)")
    else:
        scripts["test"] = "vitest run"
        pkg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        result.written.append("package.json scripts.test")


def scaffold_java(project_root: Path, result: ScaffoldResult) -> None:
    """Inject Surefire into pom.xml or append useJUnitPlatform() to build.gradle."""
    root = project_root.resolve()
    pom = root / "pom.xml"
    if pom.is_file():
        text = pom.read_text(encoding="utf-8")
        if "maven-surefire-plugin" in text:
            result.skipped.append("pom.xml (maven-surefire-plugin already present)")
        else:
            # Inject before </plugins> if possible; otherwise append before </build>
            if "</plugins>" in text:
                text = text.replace("</plugins>", _SUREFIRE_SNIPPET + "\n    </plugins>", 1)
            elif "</build>" in text:
                text = text.replace(
                    "</build>",
                    "  <plugins>" + _SUREFIRE_SNIPPET + "\n  </plugins>\n</build>",
                    1,
                )
            else:
                result.skipped.append("pom.xml (no </plugins> or </build> anchor)")
                return
            pom.write_text(text, encoding="utf-8")
            result.written.append("pom.xml (Surefire plugin)")
        return

    for gradle_name in ("build.gradle", "build.gradle.kts"):
        gradle = root / gradle_name
        if gradle.is_file():
            text = gradle.read_text(encoding="utf-8")
            if "useJUnitPlatform" in text:
                result.skipped.append(f"{gradle_name} (useJUnitPlatform already present)")
            else:
                gradle.write_text(text + _GRADLE_JUNIT_PLATFORM, encoding="utf-8")
                result.written.append(f"{gradle_name} (useJUnitPlatform())")
            return


def scaffold_configs(project_root: Path, stacks: list[StackProfile]) -> ScaffoldResult:
    """Run the applicable scaffolders **per detected stack root** and combine results.

    Each stack is scaffolded at its own ``root`` (so a monorepo ``portal/`` stack
    gets ``portal/vitest.config.ts``, not a root-level one). Result entries for a
    sub-project stack are prefixed with the relative root for disambiguation;
    root-level stacks (``root == "."``) keep their bare names. Deduplicated by
    ``(language, root)``.
    """
    result = ScaffoldResult()
    base = project_root.resolve()
    seen: set[tuple[str, str]] = set()
    scaffolders = {
        "python": scaffold_python,
        "js_ts": scaffold_js_ts,
        "java": scaffold_java,
    }
    for stack in stacks:
        key = (stack.language, stack.root)
        if key in seen:
            continue
        seen.add(key)
        scaffolder = scaffolders.get(stack.language)
        if scaffolder is None:
            continue
        stack_root = base if stack.root == "." else (base / stack.root)
        before_w, before_s = len(result.written), len(result.skipped)
        scaffolder(stack_root, result)
        if stack.root != ".":
            prefix = stack.root.rstrip("/") + "/"
            result.written[before_w:] = [prefix + n for n in result.written[before_w:]]
            result.skipped[before_s:] = [prefix + n for n in result.skipped[before_s:]]
    return result


# ---------------------------------------------------------------------------
# 8-section test-architecture.md renderer
# ---------------------------------------------------------------------------

#: Canonical section titles in strict order.
SECTION_TITLES: tuple[str, ...] = (
    "Stack profile",
    "Test cone target",
    "Layer 1 — Unit",
    "Layer 2 — Contract",
    "Layer 3 — Integration",
    "Layer 4 — E2E",
    "Test data strategy",
    "CI integration",
)

#: JSON payload keys that map to each section (in the same order).
SECTION_KEYS: tuple[str, ...] = (
    "stack_profile",
    "test_cone_target",
    "layer_unit",
    "layer_contract",
    "layer_integration",
    "layer_e2e",
    "test_data_strategy",
    "ci_integration",
)


def _render_section(
    title: str, body: str, rationale: str, *, na_text: str = "_Not applicable for this stack._"
) -> str:
    """Render one canonical section with its Rationale ADR block."""
    lines = [
        f"## {title}",
        "",
        body.strip(),
        "",
        "### Rationale",
        "",
        rationale.strip() if rationale.strip() else na_text,
        "",
    ]
    return "\n".join(lines)


#: The 9 canonical data-test-architecture sections, in order (DT-1.2, #1135).
DATA_SECTION_TITLES: tuple[str, ...] = (
    "Data stack profile",
    "Data-test cone target",
    "Schema & migration tests",
    "Referential integrity & constraints",
    "Data quality assertions",
    "ETL / pipeline correctness",
    "Deterministic test data",
    "Migration data parity",
    "CI integration",
)

#: JSON payload keys for each data section (same order as the titles).
DATA_SECTION_KEYS: tuple[str, ...] = (
    "data_stack_profile",
    "data_cone_target",
    "schema_migration",
    "referential_integrity",
    "data_quality",
    "etl_pipeline",
    "deterministic_data",
    "migration_parity",
    "ci_integration",
)


def render_data_test_architecture(payload: dict[str, object], backend: str) -> str:
    """Render the 9-section ``data-test-architecture-<backend>.md`` for one backend (#1135).

    Consumes ``payload["data_architectures"][backend]`` — a map of the
    :data:`DATA_SECTION_KEYS` to ``{body, rationale}`` (or a plain string body). A
    category that does not apply to the backend renders ``_Not applicable for this
    backend._`` (e.g. relational schema/migration on DynamoDB).
    """
    project_name = str(payload.get("project_name") or "Project")
    archs = payload.get("data_architectures")
    archs = archs if isinstance(archs, dict) else {}
    entry = archs.get(backend)
    sections: dict[str, object] = entry if isinstance(entry, dict) else {}

    header = "\n".join(
        [
            f"# Data-test Architecture — {_safe_filename(project_name)} ({backend})",
            "",
            f"**Data backend:** {backend}",
            "",
        ]
    )

    blocks: list[str] = []
    for title, key in zip(DATA_SECTION_TITLES, DATA_SECTION_KEYS, strict=True):
        sec = sections.get(key)
        if isinstance(sec, dict):
            body = str(sec.get("body") or "")
            rationale = str(sec.get("rationale") or "")
        elif isinstance(sec, str):
            body, rationale = sec, ""
        else:
            body, rationale = "_Not applicable for this backend._", ""
        blocks.append(
            _render_section(title, body, rationale, na_text="_Not applicable for this backend._")
        )
    return header + "\n---\n\n".join(blocks)


def render_test_architecture(
    payload: dict[str, object],
    stacks: list[StackProfile],
) -> str:
    """Render the full 8-section ``test-architecture.md`` from *payload* + detected stacks."""
    project_name = str(payload.get("project_name") or "Project")
    raw_cov = payload.get("coverage_target", 80)
    coverage_target = int(raw_cov) if isinstance(raw_cov, int | float) else 80
    cloud_target = str(payload.get("cloud_target") or "none")

    stack_list = (
        ", ".join(
            s.language.replace("_", "/") + ("" if s.root == "." else f" ({s.root})") for s in stacks
        )
        or "unknown"
    )

    header_lines = [
        f"# Test Architecture — {_safe_filename(project_name)}",
        "",
        f"**Cloud target:** {cloud_target}  ",
        f"**Coverage target:** {coverage_target}%  ",
        f"**Detected stacks:** {stack_list}",
        "",
    ]
    header = "\n".join(header_lines)

    section_blocks: list[str] = []
    sections_payload = payload.get("sections")
    sections: dict[str, object] = sections_payload if isinstance(sections_payload, dict) else {}

    for title, key in zip(SECTION_TITLES, SECTION_KEYS, strict=True):
        entry = sections.get(key)
        if isinstance(entry, dict):
            body = str(entry.get("body") or "")
            rationale = str(entry.get("rationale") or "")
        elif isinstance(entry, str):
            body = entry
            rationale = ""
        else:
            body = f"_No {key} data provided._"
            rationale = ""
        section_blocks.append(_render_section(title, body, rationale))

    return header + "\n---\n\n".join(section_blocks)


# ---------------------------------------------------------------------------
# Scoped-analysis mode: validate and write s1-findings.json
# ---------------------------------------------------------------------------

#: Required top-level keys for a valid s1-findings payload.
_S1_FINDINGS_REQUIRED_KEYS: tuple[str, ...] = ("schema_version", "conventions", "gaps")

#: Required convention sub-keys.
_S1_CONVENTIONS_REQUIRED_KEYS: tuple[str, ...] = (
    "framework",
    "layout",
    "naming",
    "fixture_mock_idioms",
    "assertion_style",
)

#: Allowed gap_type values (mirrors the JSON Schema enum).
_S1_GAP_TYPES: frozenset[str] = frozenset(
    {
        "missing_negative_test",
        "missing_edge_case",
        "untested_symbol",
        "test_smell",
        "missing_contract_test",
        "other",
    }
)


def validate_s1_findings(findings: object) -> list[str]:
    """Structural validation for a parsed s1-findings payload.

    Returns a list of error strings.  An empty list means the payload is valid.
    The model supplies the content; this function only checks structural integrity
    (required keys, types, enum values) so that S2 can trust what it reads.

    Composition seam: S1 (skill-first, in-session) emits the raw JSON; this
    function is the deterministic gate before S2 consumes the artifact.
    """
    errors: list[str] = []
    if not isinstance(findings, dict):
        return ["s1-findings must be a JSON object"]

    for key in _S1_FINDINGS_REQUIRED_KEYS:
        if key not in findings:
            errors.append(f"missing required key: {key!r}")

    if errors:
        return errors

    # schema_version
    if findings.get("schema_version") != "1.0":
        errors.append("schema_version must be '1.0'")

    # conventions
    conventions = findings.get("conventions")
    if not isinstance(conventions, dict):
        errors.append("'conventions' must be an object")
    else:
        for key in _S1_CONVENTIONS_REQUIRED_KEYS:
            if key not in conventions:
                errors.append(f"conventions missing required key: {key!r}")
            elif not isinstance(conventions[key], str):
                errors.append(f"conventions[{key!r}] must be a string")

    # gaps
    gaps = findings.get("gaps")
    if not isinstance(gaps, list):
        errors.append("'gaps' must be an array")
    else:
        for i, gap in enumerate(gaps):
            if not isinstance(gap, dict):
                errors.append(f"gaps[{i}] must be an object")
                continue
            for req in ("symbol", "gap_type", "description"):
                if req not in gap:
                    errors.append(f"gaps[{i}] missing required key: {req!r}")
                elif not isinstance(gap[req], str):
                    errors.append(f"gaps[{i}][{req!r}] must be a string")
            gt = gap.get("gap_type")
            if isinstance(gt, str) and gt not in _S1_GAP_TYPES:
                errors.append(
                    f"gaps[{i}]['gap_type'] {gt!r} not in allowed set: "
                    + ", ".join(sorted(_S1_GAP_TYPES))
                )

    return errors


def write_s1_findings(findings: dict[str, object], run_dir: Path) -> Path:
    """Validate *findings* and write ``s1-findings.json`` under *run_dir*.

    Returns the path written.  Raises ``ValueError`` when the payload fails
    structural validation.

    *run_dir* is created if it does not exist.  The write is atomic: the
    payload is serialised to a string first, then written in one call so a
    partial write cannot leave a corrupt artifact.
    """
    errors = validate_s1_findings(findings)
    if errors:
        raise ValueError("s1-findings validation failed:\n" + "\n".join(f"  - {e}" for e in errors))

    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / "s1-findings.json"
    out_path.write_text(
        json.dumps(findings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911, PLR0912, PLR0915
    parser = argparse.ArgumentParser(
        description="Deterministic sdet-architect: detect stacks, scaffold configs, render doc."
    )
    parser.add_argument(
        "--payload",
        help="Model JSON payload path, or - for stdin.  Omit for --detect-only.",
    )
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root to scan and scaffold (default: cwd).",
    )
    parser.add_argument(
        "--output",
        help="Output path for test-architecture.md (default: <project-root>/test-architecture.md).",
    )
    parser.add_argument(
        "--detect-only",
        action="store_true",
        help="Only detect stacks; print JSON and exit (no scaffolding, no doc).",
    )
    parser.add_argument(
        "--data-backend",
        help=(
            "Override data-backend detection (comma-separated, closed set: "
            f"{', '.join(DATA_BACKENDS)}). Omit to auto-detect from dependency manifests."
        ),
    )
    parser.add_argument(
        "--scaffold-only",
        action="store_true",
        help="Only scaffold configs; print JSON result and exit (no doc rendering).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rendered doc to stdout; do not write any files.",
    )
    parser.add_argument(
        "--scoped-findings",
        help=(
            "Path to the model-emitted s1-findings.json payload (scoped-analysis mode). "
            "Validates structure and writes the artifact to --run-dir."
        ),
    )
    parser.add_argument(
        "--run-dir",
        help=(
            "Run directory for scoped-analysis output (default: cwd). "
            "Required with --scoped-findings."
        ),
    )
    args = parser.parse_args(argv)

    # -------------------------------------------------------------------------
    # Scoped-analysis mode: validate and write s1-findings.json
    # -------------------------------------------------------------------------
    if args.scoped_findings:
        findings_path = Path(args.scoped_findings)
        if not findings_path.is_file():
            print(f"Error: --scoped-findings {findings_path!r} not found", file=sys.stderr)
            return 2
        try:
            raw_findings = findings_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading --scoped-findings: {exc}", file=sys.stderr)
            return 2
        try:
            findings_obj: object = json.loads(raw_findings)
        except json.JSONDecodeError as exc:
            print(f"Error: --scoped-findings is not valid JSON: {exc}", file=sys.stderr)
            return 2

        run_dir = Path(args.run_dir).resolve() if args.run_dir else Path.cwd()
        try:
            out_path = write_s1_findings(findings_obj, run_dir)  # type: ignore[arg-type]
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

        sys.stdout.write(
            json.dumps(
                {"status": "ok", "s1_findings": str(out_path)},
                indent=2,
            )
            + "\n"
        )
        return 0

    project_root = Path(args.project_root).resolve()
    if not project_root.is_dir():
        print(f"Error: --project-root {project_root!r} is not a directory", file=sys.stderr)
        return 2

    stacks = detect_stacks(project_root)

    # Resolve data backends (override or inline detection) for the data-testing
    # strategy (DT-1.1, #1134). A bad --data-backend value is an input error.
    try:
        data_backends = resolve_data_backends(project_root, args.data_backend)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 3

    if args.detect_only:
        sys.stdout.write(
            json.dumps(
                {
                    "project_root": str(project_root),
                    "stacks": [
                        {
                            "language": s.language,
                            "manifest_files": s.manifest_files,
                            "root": s.root,
                        }
                        for s in stacks
                    ],
                    "data_backends": data_backends,
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    if args.scaffold_only:
        scaffold_result = scaffold_configs(project_root, stacks)
        sys.stdout.write(
            json.dumps(
                {"written": scaffold_result.written, "skipped": scaffold_result.skipped},
                indent=2,
            )
            + "\n"
        )
        return 0

    # Full run: need the payload
    if not args.payload:
        print(
            "Error: --payload is required unless --detect-only or --scaffold-only",
            file=sys.stderr,
        )
        return 2

    if args.payload == "-":
        raw = sys.stdin.read()
    else:
        payload_path = Path(args.payload)
        if not payload_path.is_file():
            print(f"Error: --payload {payload_path!r} not found", file=sys.stderr)
            return 2
        raw = payload_path.read_text(encoding="utf-8")

    try:
        payload: dict[str, object] = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --payload JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("Error: --payload must be a JSON object", file=sys.stderr)
        return 2

    if not stacks:
        print(
            json.dumps(
                {
                    "project_root": str(project_root),
                    "stacks": [],
                    "error": "no stacks detected — cannot render test architecture",
                },
                indent=2,
            )
        )
        return 1

    # One document PER detected stack (per (language, root) pair), placed under each
    # stack's own root — a polyglot project gets multiple docs (#1093). Deduped by
    # (language, root), preserving detection order.
    unique_stacks: list[StackProfile] = []
    seen_keys: set[tuple[str, str]] = set()
    for s in stacks:
        key = (s.language, s.root)
        if key not in seen_keys:
            seen_keys.add(key)
            unique_stacks.append(s)

    # Per-backend data-test-architecture docs (DT-1.2, #1135) — only for backends the
    # model supplied a `data_architectures` entry for.
    data_archs = payload.get("data_architectures")
    data_archs = data_archs if isinstance(data_archs, dict) else {}
    emit_backends = [b for b in data_backends if b in data_archs]

    if args.dry_run:
        # Render each stack's doc + each backend's data doc to stdout.
        docs = [render_test_architecture(payload, [s]) for s in unique_stacks]
        docs += [render_data_test_architecture(payload, b) for b in emit_backends]
        sys.stdout.write("\n\n---\n\n".join(docs))
        return 0

    # Scaffold configs (only when not dry-run)
    scaffold_result = scaffold_configs(project_root, stacks)

    project_name = str(payload.get("project_name") or "project")
    safe_name = _safe_filename(project_name)
    # --output overrides the path only for a single-stack project; with multiple
    # stacks the per-root default is used (one --output path cannot name N docs).
    single_stack = len(unique_stacks) == 1

    written_docs: list[str] = []
    for stack in unique_stacks:
        doc = render_test_architecture(payload, [stack])

        if args.output and single_stack:
            out_path = Path(args.output)
        else:
            stack_dir = "" if stack.root == "." else stack.root.rstrip("/") + "/"
            out_path = project_root / f"{stack_dir}test-architecture-{safe_name}.md"

        try:
            if not out_path.is_absolute():
                out_path = _safe_output_path(project_root, str(out_path))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(doc, encoding="utf-8")
        written_docs.append(str(out_path))

    # Write one data-test-architecture-<backend>.md per supplied backend (#1135),
    # idempotent + containment-guarded like the per-stack docs.
    data_docs: list[str] = []
    for backend in emit_backends:
        data_doc = render_data_test_architecture(payload, backend)
        try:
            data_path = _safe_output_path(project_root, f"data-test-architecture-{backend}.md")
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        data_path.write_text(data_doc, encoding="utf-8")
        data_docs.append(str(data_path))

    sys.stdout.write(
        json.dumps(
            {
                "project_root": str(project_root),
                "stacks": [{"language": s.language, "root": s.root} for s in unique_stacks],
                "scaffold": {
                    "written": scaffold_result.written,
                    "skipped": scaffold_result.skipped,
                },
                "test_architecture_md": written_docs,
                "data_test_architecture_md": data_docs,
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

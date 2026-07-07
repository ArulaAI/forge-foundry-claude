"""FM1 (hallucinated dependencies) detector for Python.

Vendored from forge-prowess-primitives@v0.4.0 (auditor.py:_audit_fm1) — see
CHANGELOG-VENDOR.md for divergence. Foundry refactors:
- Extracted into per-stack module so adding Rust/Go is a one-file addition.
- read_declared_deps signature unchanged.
- audit_python_fm1 takes AuditConfig so project.ok_toplevels extends the OK set.
"""

from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import DEFAULT_EXCLUDED_DIRS, walk_source_files
from primitives.failure_mode_audit.shared.findings import Finding

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.failure_mode_audit.shared.config import AuditConfig

# Foundry's own packages — always OK as imports inside foundry-vendored code.
_FOUNDRY_OK_TOPLEVELS: frozenset[str] = frozenset(
    ["forge_validate", "forge_sync", "primitives", "tests", "_pytest", "pytest"]
)


def _normalize_pkg(name: str) -> str:
    """Normalize a package/distribution name to its import-namespace form."""
    return name.lower().replace("-", "_")


def discover_first_party_toplevels(project_root: Path) -> set[str]:
    """Detect the repo's own top-level import packages (#1192).

    Without this, a project's own top-level packages (e.g. ``billing_engine``,
    ``api``) are flagged as "possible hallucinated dependency" because they are
    not listed under pyproject ``dependencies`` — they *are* the project.

    Deterministic heuristics:
      * flat layout — an immediate child directory of *project_root* holding an
        ``__init__.py`` is a first-party package;
      * src layout — every immediate child of ``src/`` (package dir or top-level
        ``.py`` module) is first-party;
      * the distribution ``[project].name`` from pyproject.toml.
    Excluded/dependency dir names (``.venv``, ``node_modules``, …) are skipped.
    """
    names: set[str] = set()

    # Flat layout: top-level packages directly under the project root.
    if project_root.is_dir():
        for child in project_root.iterdir():
            if child.name in DEFAULT_EXCLUDED_DIRS:
                continue
            if child.is_dir() and (child / "__init__.py").is_file():
                names.add(_normalize_pkg(child.name))

    # src layout: everything directly under src/ is first-party — but only when
    # src/ is a genuine layout root (no __init__.py of its own; otherwise `src`
    # is itself a package, already caught by the flat-layout scan above).
    src = project_root / "src"
    if src.is_dir() and not (src / "__init__.py").is_file():
        for child in src.iterdir():
            if child.name in DEFAULT_EXCLUDED_DIRS:
                continue
            if child.is_dir():
                names.add(_normalize_pkg(child.name))
            elif child.suffix == ".py":
                names.add(_normalize_pkg(child.stem))

    # Distribution name from [project].name (covers single-module projects).
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            content = ""
        match = re.search(r'(?m)^\s*name\s*=\s*"([^"]+)"', content)
        if match:
            names.add(_normalize_pkg(match.group(1)))

    return names


def extract_imports(content_line: str) -> set[str]:
    """Extract top-level package names from a single Python import line."""
    packages: set[str] = set()
    line = content_line.split("#", 1)[0].strip()
    if line.startswith("import "):
        mod = line.removeprefix("import ").split(" as ")[0].strip().split(",")[0].strip()
        if mod:
            packages.add(mod.split(".")[0])
    elif line.startswith("from ") and " import " in line:
        parts = line.split()
        if len(parts) >= 2:  # noqa: PLR2004
            packages.add(parts[1].split(".")[0])
    return packages


def read_declared_deps(project_root: Path) -> set[str]:
    """Read declared deps from pyproject.toml + requirements.txt + stdlib + foundry OKs.

    Also resolves the project's own first-party top-level packages (#1192) so
    imports of the repo's own code are not flagged as hallucinated dependencies.
    """
    deps: set[str] = set(sys.stdlib_module_names)
    deps.update(_FOUNDRY_OK_TOPLEVELS)
    deps.update(discover_first_party_toplevels(project_root))

    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        content = pyproject.read_text(encoding="utf-8")
        match = re.search(r"dependencies\s*=\s*\[([^\]]*)\]", content, re.S)
        if match:
            for raw in re.findall(r'"([^"]+)"', match.group(1)):
                name = re.split(r"[<>=!~ ]", raw)[0].lower().replace("-", "_")
                deps.add(name)

    reqs = project_root / "requirements.txt"
    if reqs.is_file():
        for raw in reqs.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#"):
                name = re.split(r"[<>=!~ ]", stripped)[0].lower().replace("-", "_")
                deps.add(name)

    return deps


def audit_python_fm1(
    path: Path,
    *,
    project_root: Path,
    config: AuditConfig,
) -> list[Finding]:
    """Audit *path* for FM1 (hallucinated deps), Python files only.

    Args:
        path: Directory or single .py file to audit.
        project_root: Root of the project (where pyproject.toml lives).
        config: AuditConfig — project.ok_toplevels extends the OK whitelist.

    Returns:
        List of Finding entries, one per import that is not in the declared deps
        + stdlib + project ok_toplevels + foundry OKs.
    """
    findings: list[Finding] = []
    declared = read_declared_deps(project_root)
    declared.update(t.lower().replace("-", "_") for t in config.project.ok_toplevels)

    py_files = list(walk_source_files(path, {".py"}, include_vendored=config.include_vendored))
    for py_file in py_files:
        try:
            content = py_file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue
        for line_num, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            for pkg in extract_imports(stripped):
                pkg_norm = pkg.lower()
                if pkg_norm not in declared:
                    findings.append(
                        Finding(
                            mode="fm1",
                            severity="medium",
                            file=str(py_file),
                            line=line_num,
                            message=(
                                f"Import `{pkg}` not in any declared manifest "
                                "(possible hallucinated dependency)"
                            ),
                            snippet=stripped,
                            rule_id="fm1.python.import-not-in-deps",
                        )
                    )
    return findings

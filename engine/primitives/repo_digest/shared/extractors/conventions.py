"""Conventions extractor — generates conventions.md content.

Produces a structured markdown string covering:
  - Lint / format config (ruff, ESLint, Checkstyle, Prettier)
  - Test framework per detected language (via _sdet stacks)
  - Naming patterns sampled from top-ranked source files

Output is truncated to config.budget.per_file.conventions tokens via enforce_budget.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from primitives._sdet.stacks import all_stacks
from primitives._shared.scan_exclusions import walk_source_files as _walk_source_files
from primitives.repo_digest.shared.budget import enforce_budget
from primitives.repo_digest.shared.ranking import rank_files

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import DigestConfig

# ---------------------------------------------------------------------------
# Lint / format detection helpers
# ---------------------------------------------------------------------------


def _detect_ruff(project_root: Path) -> bool:
    """Return True if a ruff config is present."""
    if (project_root / ".ruff.toml").is_file() or (project_root / "ruff.toml").is_file():
        return True
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8")
            if re.search(r"^\[tool\.ruff", content, re.MULTILINE):
                return True
        except (UnicodeDecodeError, PermissionError):
            pass
    return False


def _read_ruff_version(project_root: Path) -> str:
    """Extract ruff version from pyproject.toml deps if present, else 'auto'."""
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8")
            # Look for ruff in dependency sections
            match = re.search(r'"ruff(?:[<>=!~][^"]*)?"\s*', content)
            if match:
                ver_match = re.search(r"[<>=!~]+\s*([0-9][0-9.]*)", match.group(0))
                if ver_match:
                    return ver_match.group(1)
        except (UnicodeDecodeError, PermissionError):
            pass
    return "auto"


def _detect_eslint(project_root: Path) -> bool:
    """Return True if an ESLint config is present."""
    eslint_filenames = (
        ".eslintrc.json",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.yaml",
        ".eslintrc.yml",
        ".eslintrc",
    )
    for name in eslint_filenames:
        if (project_root / name).is_file():
            return True
    # Check eslintConfig key in package.json
    pkg = project_root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "eslintConfig" in data:
                return True
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    return False


def _detect_checkstyle(project_root: Path) -> bool:
    """Return True if a checkstyle config is present."""
    if (project_root / "checkstyle.xml").is_file():
        return True
    return bool((project_root / "config" / "checkstyle.xml").is_file())


def _detect_prettier(project_root: Path) -> bool:
    """Return True if a Prettier config is present."""
    prettier_patterns = (
        ".prettierrc",
        ".prettierrc.json",
        ".prettierrc.js",
        ".prettierrc.cjs",
        ".prettierrc.yaml",
        ".prettierrc.yml",
        ".prettierrc.toml",
        "prettier.config.js",
        "prettier.config.cjs",
        "prettier.config.ts",
    )
    return any((project_root / name).is_file() for name in prettier_patterns)


def _build_lint_section(project_root: Path) -> list[str]:
    """Build lines for the ## Lint / format config section."""
    lines: list[str] = ["## Lint / format config", ""]
    items: list[str] = []

    if _detect_ruff(project_root):
        version = _read_ruff_version(project_root)
        items.append(f"- ruff ({version})")

    if _detect_eslint(project_root):
        items.append("- ESLint")

    if _detect_checkstyle(project_root):
        items.append("- Checkstyle")

    if _detect_prettier(project_root):
        items.append("- Prettier")

    if items:
        lines.extend(items)
    else:
        lines.append("_None detected._")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Test framework detection helpers
# ---------------------------------------------------------------------------


def _build_test_section(project_root: Path) -> list[str]:
    """Build lines for the ## Test framework section."""
    lines: list[str] = ["## Test framework", ""]
    # FLEX-2 (#174): iterate the stack registry instead of name-importing per-language
    # detectors. A new stack module that self-registers gets picked up here for free.
    items: list[str] = []
    for _name, detect_fn in all_stacks().items():
        profile = detect_fn(project_root)
        if profile is None or profile.framework == "none":
            continue
        label = profile.language.capitalize()
        ver = f" {profile.framework_version}" if profile.framework_version else ""
        items.append(f"- {label}: {profile.framework}{ver}")

    if items:
        lines.extend(items)
    else:
        lines.append("_None detected._")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Naming patterns helpers
# ---------------------------------------------------------------------------

_PY_DEF_RE = re.compile(r"^def\s+([a-zA-Z_]\w*)\s*\(", re.MULTILINE)
_PY_CLASS_RE = re.compile(r"^class\s+([A-Z]\w*)\s*[:\(]", re.MULTILINE)
_TS_FUNC_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+([a-zA-Z_$][\w$]*)\s*[<\(]",
    re.MULTILINE,
)
_TS_CLASS_RE = re.compile(r"^(?:export\s+)?class\s+([A-Z][\w]*)\b", re.MULTILINE)
_JAVA_CLASS_RE = re.compile(
    r"^\s*(?:public\s+)?(?:abstract\s+)?class\s+([A-Z][\w]*)\b", re.MULTILINE
)
_JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|protected|private)\s+(?:static\s+)?(?:\w+\s+)+([a-z][\w]*)\s*\(",
    re.MULTILINE,
)


def _extract_names_from_py(content: str) -> list[str]:
    names: list[str] = []
    for m in _PY_CLASS_RE.finditer(content):
        names.append(m.group(1))
    for m in _PY_DEF_RE.finditer(content):
        name = m.group(1)
        if not name.startswith("_"):
            names.append(name)
    return names


def _extract_names_from_ts(content: str) -> list[str]:
    names: list[str] = []
    for m in _TS_CLASS_RE.finditer(content):
        names.append(m.group(1))
    for m in _TS_FUNC_RE.finditer(content):
        names.append(m.group(1))
    return names


def _extract_names_from_java(content: str) -> list[str]:
    names: list[str] = []
    for m in _JAVA_CLASS_RE.finditer(content):
        names.append(m.group(1))
    for m in _JAVA_METHOD_RE.finditer(content):
        names.append(m.group(1))
    return names


def _collect_source_files(
    project_root: Path,
    extensions: set[str],
    *,
    include_vendored: bool = False,
) -> list[Path]:
    """Collect source files with given extensions, skipping excluded dirs."""
    return list(_walk_source_files(project_root, extensions, include_vendored=include_vendored))


_MAX_SAMPLE_IDENTIFIERS = 10
_MAX_NAMES_PER_FILE = 3
_TOP_FILES_COUNT = 5


def _extract_names_from_file(f: Path) -> list[str]:
    """Read *f* and extract public identifiers based on file extension."""
    try:
        content = f.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []
    if f.suffix == ".py":
        return _extract_names_from_py(content)
    if f.suffix in (".ts", ".tsx"):
        return _extract_names_from_ts(content)
    if f.suffix == ".java":
        return _extract_names_from_java(content)
    return []


def _sample_identifiers(top_files: list[Path]) -> list[str]:
    """Collect up to _MAX_SAMPLE_IDENTIFIERS unique identifiers from top_files."""
    seen: set[str] = set()
    unique: list[str] = []
    for f in top_files:
        for name in _extract_names_from_file(f)[:_MAX_NAMES_PER_FILE]:
            if name not in seen:
                seen.add(name)
                unique.append(name)
            if len(unique) >= _MAX_SAMPLE_IDENTIFIERS:
                return unique
    return unique


def _build_naming_section(
    project_root: Path, config: DigestConfig, *, include_vendored: bool = False
) -> list[str]:
    """Build lines for the ## Naming patterns section."""
    lines: list[str] = ["## Naming patterns", ""]

    # Gather source files by extension
    py_files = _collect_source_files(project_root, {".py"}, include_vendored=include_vendored)
    ts_files = _collect_source_files(
        project_root, {".ts", ".tsx"}, include_vendored=include_vendored
    )
    java_files = _collect_source_files(project_root, {".java"}, include_vendored=include_vendored)

    all_source = py_files + ts_files + java_files
    if not all_source:
        lines.append("_No source files detected._")
        lines.append("")
        return lines

    # Rank the combined set and take top files; then extract names per extension
    ranked = rank_files(
        all_source,
        project_root=project_root,
        weights=config.ranking_weights,
        days=config.extractors.recent_deltas.days,
    )
    top_files = [f for f, _ in ranked[:_TOP_FILES_COUNT]]
    unique = _sample_identifiers(top_files)

    if unique:
        lines.append("Representative identifiers sampled from top-ranked source files:")
        lines.append("")
        for name in unique:
            lines.append(f"- `{name}`")
    else:
        lines.append("_No public identifiers found in top source files._")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_conventions(
    project_root: Path,
    *,
    config: DigestConfig,
    include_vendored: bool = False,
) -> str:
    """Generate conventions.md markdown content.

    Sections produced:
        # Conventions
        ## Lint / format config
        - ruff (version)
        - ESLint
        - Checkstyle
        - Prettier
        (_None detected._ if absent)
        ## Test framework
        - Python: pytest <version>
        - TypeScript: vitest
        - Java: junit-jupiter
        ## Naming patterns
        Representative identifiers sampled from top-ranked source files.

    Honors config.budget.per_file.conventions token cap (uses enforce_budget).
    """
    lines: list[str] = ["# Conventions", ""]

    lines.extend(_build_lint_section(project_root))
    lines.extend(_build_test_section(project_root))
    lines.extend(_build_naming_section(project_root, config, include_vendored=include_vendored))

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.conventions)

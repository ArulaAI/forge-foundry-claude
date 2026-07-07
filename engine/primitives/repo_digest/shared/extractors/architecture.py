"""Architecture extractor — generates repo-digest.md content.

Produces a structured markdown string covering:
  - Project name (from pyproject.toml / package.json / directory name)
  - Languages detected by extension
  - Frameworks detected via _sdet stack detectors
  - Purpose (first non-heading paragraph of README)
  - Top-level directory structure with role hints

Output is truncated to config.budget.per_file.repo_digest tokens via enforce_budget.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from primitives._sdet.stacks import all_stacks
from primitives._shared.scan_exclusions import DEFAULT_EXCLUDED_DIRS
from primitives._shared.scan_exclusions import is_excluded as _is_excluded
from primitives.repo_digest.shared.budget import enforce_budget

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import DigestConfig

_DIR_ROLE_HINTS: dict[str, str] = {
    "src": "source code",
    "lib": "library code",
    "app": "application code",
    "tests": "test suite",
    "test": "test suite",
    "spec": "specifications",
    "docs": "documentation",
    "doc": "documentation",
    "scripts": "utility scripts",
    "config": "configuration",
    "configs": "configuration",
    ".github": "GitHub config (workflows, issue templates)",
    "infrastructure": "deployment + infra config",
    "infra": "deployment + infra config",
    "services": "service modules",
    "primitives": "foundry primitives",
    "adapters": "harness adapters",
}

_LANG_EXTENSIONS: dict[str, set[str]] = {
    "python": {".py"},
    "typescript": {".ts", ".tsx"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "java": {".java"},
}


def _detect_languages(project_root: Path, *, include_vendored: bool = False) -> dict[str, int]:
    counts: dict[str, int] = {lang: 0 for lang in _LANG_EXTENSIONS}
    for f in project_root.rglob("*"):
        if not f.is_file():
            continue
        if _is_excluded(f, project_root, include_vendored=include_vendored):
            continue
        for lang, exts in _LANG_EXTENSIONS.items():
            if f.suffix in exts:
                counts[lang] += 1
                break
    return {k: v for k, v in counts.items() if v > 0}


def _read_project_name(project_root: Path) -> str:
    # Try pyproject.toml [project].name first
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            content = pyproject.read_text(encoding="utf-8")
            m = re.search(r'^name\s*=\s*"([^"]+)"', content, re.MULTILINE)
            if m:
                return m.group(1)
        except (UnicodeDecodeError, PermissionError):
            pass
    # Try package.json
    pkg = project_root / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if isinstance(data.get("name"), str):
                return str(data["name"])
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
    # Fall back to directory name
    return project_root.name


def _read_purpose(project_root: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = project_root / name
        if readme.is_file():
            try:
                content = readme.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue
            # Skip the first heading line; take the first non-heading paragraph
            paragraphs: list[str] = []
            current: list[str] = []
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped:
                    if current:
                        paragraphs.append(" ".join(current))
                        current = []
                    continue
                if stripped.startswith("#"):
                    if current:
                        paragraphs.append(" ".join(current))
                        current = []
                    continue
                current.append(stripped)
            if current:
                paragraphs.append(" ".join(current))
            if paragraphs:
                return paragraphs[0]
    return "_No README found._"


def _top_level_dirs(project_root: Path) -> list[tuple[str, str]]:
    """Return list of (dir_name, role_description) tuples for top-level directories."""
    result: list[tuple[str, str]] = []
    for entry in sorted(project_root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") and entry.name != ".github":
            continue
        if entry.name in DEFAULT_EXCLUDED_DIRS:
            continue
        role = _DIR_ROLE_HINTS.get(entry.name, "—")
        result.append((entry.name, role))
    return result


def extract_architecture(
    project_root: Path,
    *,
    config: DigestConfig,
    include_vendored: bool = False,
) -> str:
    """Generate the repo-digest.md content (markdown string).

    Sections produced:
        # Project: <name>
        ## Languages
        - python (~N files)
        - typescript (~N files)
        ## Purpose
        <first paragraph of README.md, or "_No README found._">
        ## Top-level structure
        - src/ — source code
        - tests/ — test suite
        - docs/ — documentation
        - etc.

    Honors config.budget.per_file.repo_digest token cap (uses enforce_budget).
    """
    name = _read_project_name(project_root)
    purpose = _read_purpose(project_root)
    languages = _detect_languages(project_root, include_vendored=include_vendored)
    structure = _top_level_dirs(project_root)

    # FLEX-2 (#174): iterate the stack registry — plugin-friendly.
    framework_lines: list[str] = []
    for _name, detect_fn in all_stacks().items():
        profile = detect_fn(project_root)
        if profile is None or profile.framework == "none":
            continue
        label = profile.language.capitalize()
        fw = profile.framework
        fw_ver = profile.framework_version or ""
        framework_lines.append(f"- {label}: {fw} {fw_ver}".rstrip())

    lines: list[str] = [
        f"# Project: {name}",
        "",
        "## Languages",
        "",
    ]
    if languages:
        for lang, count in sorted(languages.items(), key=lambda x: -x[1]):
            lines.append(f"- {lang} ({count} files)")
    else:
        lines.append("_No source files detected._")
    lines.append("")

    if framework_lines:
        lines.append("## Frameworks")
        lines.append("")
        lines.extend(framework_lines)
        lines.append("")

    lines.append("## Purpose")
    lines.append("")
    lines.append(purpose)
    lines.append("")

    lines.append("## Top-level structure")
    lines.append("")
    if structure:
        for name_, role in structure:
            lines.append(f"- `{name_}/` — {role}")
    else:
        lines.append("_No top-level directories detected._")
    lines.append("")

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.repo_digest)

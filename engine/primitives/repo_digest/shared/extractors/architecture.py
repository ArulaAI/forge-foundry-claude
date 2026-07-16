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
from primitives.repo_digest.shared.extractors.data_access import extract_data_access

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

# Display labels for the ``## LOC by Language`` table.  These MUST match the
# capitalized keys the SP3 app-digest aggregator keys its flavor fallback on
# (``"Java"``/``"Python"``/``"JavaScript"``/``"TypeScript"``); see #1405 and
# ``app_digest_aggregator.assemble``'s ``loc_by_language`` branch.
_LANG_LABELS: dict[str, str] = {
    "python": "Python",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "java": "Java",
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


def _count_lines(path: Path) -> int:
    """Return the number of newline-terminated lines in *path* (0 on read error)."""
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except (OSError, PermissionError):
        return 0


def _loc_by_language(project_root: Path, *, include_vendored: bool = False) -> dict[str, int]:
    """Return {DisplayLabel: total-source-lines} across detected languages.

    Unlike :func:`_detect_languages` (which counts *files*), this counts *lines*
    so the ``## LOC by Language`` table carries real LOC weights — the shape the
    SP3 aggregator parses into ``loc_by_language`` (#1405).
    """
    loc: dict[str, int] = dict.fromkeys(_LANG_EXTENSIONS, 0)
    for f in project_root.rglob("*"):
        if not f.is_file():
            continue
        if _is_excluded(f, project_root, include_vendored=include_vendored):
            continue
        for lang, exts in _LANG_EXTENSIONS.items():
            if f.suffix in exts:
                loc[lang] += _count_lines(f)
                break
    return {_LANG_LABELS[lang]: n for lang, n in loc.items() if n > 0}


def _module_inventory(
    project_root: Path, *, include_vendored: bool = False
) -> list[tuple[str, int]]:
    """Return [(module-name, total-source-lines), …] sorted by descending weight.

    A *module* is a top-level source directory (or top-level source file).  The
    weight is the sum of source lines beneath it.  This is the ``## Modules``
    section the SP3 aggregator's ``_parse_modules`` reads as ``- name (N lines)``
    weighted-module rows (#1405).
    """
    all_exts: set[str] = set()
    for exts in _LANG_EXTENSIONS.values():
        all_exts |= exts

    weights: dict[str, int] = {}
    for f in project_root.rglob("*"):
        if not f.is_file() or f.suffix not in all_exts:
            continue
        if _is_excluded(f, project_root, include_vendored=include_vendored):
            continue
        try:
            rel = f.relative_to(project_root)
        except ValueError:
            continue
        # First path component = the module (top-level dir, or the file stem for
        # a top-level source file).
        module = rel.parts[0] if len(rel.parts) > 1 else rel.stem
        weights[module] = weights.get(module, 0) + _count_lines(f)

    return sorted(
        ((name, n) for name, n in weights.items() if n > 0),
        key=lambda kv: (-kv[1], kv[0]),
    )


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
        ## LOC by Language
        | Language | Lines |
        |---|---|
        | Java | 12500 |
        ## Modules
        - `owners` (3200 lines)
        ## Frameworks
        ## Purpose
        <first paragraph of README.md, or "_No README found._">
        ## Top-level structure
        - src/ — source code
        - tests/ — test suite
        - docs/ — documentation
        - etc.
        ## Data Access
        - `owners`
        - `pets`

    The ``## LOC by Language``, ``## Modules``, and ``## Data Access`` sections
    are the shapes the SP3 app-digest aggregator parses (#1405): its
    ``loc_by_language`` table, its weighted ``modules`` inventory, and — most
    critically — the ``data:<table>`` join keys that thread the UI→API→DB
    correlation.  Before these sections existed, a fresh ``repo-digest`` handed
    to the aggregator recovered zero ``data:`` edges and silently severed the
    join.

    Honors config.budget.per_file.repo_digest token cap (uses enforce_budget).
    """
    name = _read_project_name(project_root)
    purpose = _read_purpose(project_root)
    languages = _detect_languages(project_root, include_vendored=include_vendored)
    loc = _loc_by_language(project_root, include_vendored=include_vendored)
    modules = _module_inventory(project_root, include_vendored=include_vendored)
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

    # ## LOC by Language — real line counts in the table shape the SP3
    # aggregator's ``_parse_loc_table`` reads (#1405).
    lines.append("## LOC by Language")
    lines.append("")
    if loc:
        lines.append("| Language | Lines |")
        lines.append("|---|---|")
        for label, count in sorted(loc.items(), key=lambda x: -x[1]):
            lines.append(f"| {label} | {count:,} |")
    else:
        lines.append("_No source lines detected._")
    lines.append("")

    # ## Modules — weighted top-level module inventory (`_parse_modules`, #1405).
    lines.append("## Modules")
    lines.append("")
    if modules:
        for mod_name, weight in modules:
            lines.append(f"- `{mod_name}` ({weight} lines)")
    else:
        lines.append("_No modules detected._")
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

    # ## Data Access — the load-bearing data:<table> join keys (#1405). The
    # extractor returns its own budgeted markdown; append it verbatim so the SP3
    # aggregator's ``_parse_consumed_tables`` can recover the edges.
    data_access = extract_data_access(
        project_root, config=config, include_vendored=include_vendored
    )
    lines.append(data_access.rstrip("\n"))
    lines.append("")

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.repo_digest)

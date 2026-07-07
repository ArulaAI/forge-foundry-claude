"""Dependency-map extractor — generates dependency-map.md content.

Produces a structured markdown string covering:
  - Internal modules (top intra-project imports by file count, per language)
  - External dependencies:
      - Python: from pyproject.toml + requirements.txt
      - Node.js: from package.json (dependencies / devDependencies / peerDependencies)
      - Java: from pom.xml (<groupId>:<artifactId> + version)

Sections without any data are omitted. Output is truncated to
config.budget.per_file.dependency_map tokens via enforce_budget.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import walk_source_files as _walk_source_files
from primitives._xml_safety import XmlSafetyError, parse_pom
from primitives.repo_digest.shared.budget import enforce_budget

if TYPE_CHECKING:
    from pathlib import Path
    from xml.etree.ElementTree import Element

    from primitives.repo_digest.shared.config import DigestConfig

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How many top internal imports to surface per language
_TOP_N = 10

# Maven XML namespace
_MVN_NS = "http://maven.apache.org/POM/4.0.0"

# Number of lines in an empty heading block (heading + blank line)
_HEADING_ONLY_LINES = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_safe(path: Path) -> str:
    """Read file text; return empty string on decode / permission error."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, FileNotFoundError):
        return ""


def _collect_files(
    project_root: Path,
    extensions: set[str],
    *,
    include_vendored: bool = False,
) -> list[Path]:
    """Walk project_root returning files with the given extensions, skipping excluded dirs."""
    return list(_walk_source_files(project_root, extensions, include_vendored=include_vendored))


# ---------------------------------------------------------------------------
# Internal import analysis
# ---------------------------------------------------------------------------

# Python: "import foo.bar" or "from foo.bar import ..."
_PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
    re.MULTILINE,
)

# TypeScript / JavaScript: import ... from '...' or require('...')
_TS_IMPORT_RE = re.compile(
    r"""(?:from\s+['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]\s*\))""",
    re.MULTILINE,
)

# Java: import com.acme.foo;
_JAVA_IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+);", re.MULTILINE)


def _is_internal_python(module: str, project_root: Path) -> bool:
    """Return True if the dotted Python module name looks like an intra-project import."""
    # Check if the top-level package exists as a directory or .py file in project_root
    top = module.split(".", maxsplit=1)[0]
    return (project_root / top).is_dir() or (project_root / f"{top}.py").is_file()


def _is_internal_ts(module: str, _project_root: Path) -> bool:
    """Return True if the TS/JS import looks like a relative or intra-project path."""
    return module.startswith(".")


def _is_internal_java(module: str, project_root: Path) -> bool:
    """Return True if the Java import looks like an intra-project class."""
    # A heuristic: check if the top-level package component matches a directory
    # under src/main/java or similar, or if pom.xml groupId is a prefix.
    top = module.split(".", maxsplit=1)[0]
    # Walk common Java source roots
    for src_root in (
        project_root / "src" / "main" / "java",
        project_root / "src",
        project_root,
    ):
        if (src_root / top).is_dir():
            return True
    # Also try groupId from pom.xml as a quick prefix check
    pom = project_root / "pom.xml"
    if pom.is_file():
        text = _read_safe(pom)
        # Extract <groupId> from the project root element (first occurrence)
        m = re.search(r"<groupId>([^<]+)</groupId>", text)
        if m:
            group_id = m.group(1).strip()
            if module.startswith(group_id):
                return True
    return False


def _build_internal_python(project_root: Path, *, include_vendored: bool = False) -> list[str]:
    """Return markdown lines for top Python internal imports by file count."""
    module_files: dict[str, set[Path]] = defaultdict(set)

    for f in _collect_files(project_root, {".py"}, include_vendored=include_vendored):
        content = _read_safe(f)
        for m in _PY_IMPORT_RE.finditer(content):
            module = m.group(1) or m.group(2)
            if not module:
                continue
            if _is_internal_python(module, project_root):
                module_files[module].add(f)

    if not module_files:
        return []

    sorted_modules = sorted(module_files.items(), key=lambda kv: -len(kv[1]))
    lines: list[str] = ["### Python", ""]
    for module, files in sorted_modules[:_TOP_N]:
        count = len(files)
        noun = "file" if count == 1 else "files"
        lines.append(f"- `{module}` ← imported by {count} {noun}")
    lines.append("")
    return lines


def _build_internal_ts(project_root: Path, *, include_vendored: bool = False) -> list[str]:
    """Return markdown lines for top TS/JS internal imports by file count."""
    module_files: dict[str, set[Path]] = defaultdict(set)

    exts = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
    for f in _collect_files(project_root, exts, include_vendored=include_vendored):
        content = _read_safe(f)
        for m in _TS_IMPORT_RE.finditer(content):
            module = m.group(1) or m.group(2)
            if not module:
                continue
            if _is_internal_ts(module, project_root):
                module_files[module].add(f)

    if not module_files:
        return []

    sorted_modules = sorted(module_files.items(), key=lambda kv: -len(kv[1]))
    lines: list[str] = ["### TypeScript / JavaScript", ""]
    for module, files in sorted_modules[:_TOP_N]:
        count = len(files)
        noun = "file" if count == 1 else "files"
        lines.append(f"- `{module}` ← imported by {count} {noun}")
    lines.append("")
    return lines


def _build_internal_java(project_root: Path, *, include_vendored: bool = False) -> list[str]:
    """Return markdown lines for top Java internal imports by file count."""
    module_files: dict[str, set[Path]] = defaultdict(set)

    for f in _collect_files(project_root, {".java"}, include_vendored=include_vendored):
        content = _read_safe(f)
        for m in _JAVA_IMPORT_RE.finditer(content):
            module = m.group(1).strip()
            if _is_internal_java(module, project_root):
                module_files[module].add(f)

    if not module_files:
        return []

    sorted_modules = sorted(module_files.items(), key=lambda kv: -len(kv[1]))
    lines: list[str] = ["### Java", ""]
    for module, files in sorted_modules[:_TOP_N]:
        count = len(files)
        noun = "file" if count == 1 else "files"
        lines.append(f"- `{module}` ← imported by {count} {noun}")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# External dependency parsing
# ---------------------------------------------------------------------------


def _parse_pyproject_toml(project_root: Path) -> list[str]:
    """Extract dependencies from pyproject.toml [project].dependencies."""
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return []

    text = _read_safe(pyproject)

    # Match the dependencies array — handles multiline TOML arrays
    # We look for the block: dependencies = [ ... ]
    deps: list[str] = []

    # Naive TOML parser for the array value — find the bracket block
    dep_block_re = re.compile(
        r"^\s*dependencies\s*=\s*\[([^\]]*)\]",
        re.MULTILINE | re.DOTALL,
    )
    for block_match in dep_block_re.finditer(text):
        block = block_match.group(1)
        for item in re.findall(r'"([^"]+)"', block):
            deps.append(item.strip())

    return deps


def _parse_requirements_txt(project_root: Path) -> list[str]:
    """Extract package specs from requirements.txt."""
    req_file = project_root / "requirements.txt"
    if not req_file.is_file():
        return []

    deps: list[str] = []
    text = _read_safe(req_file)
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        # Skip comments, blank lines, and -r includes
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        deps.append(stripped)
    return deps


def _build_external_python(project_root: Path) -> list[str]:
    """Return markdown lines for Python external dependencies."""
    toml_deps = _parse_pyproject_toml(project_root)
    req_deps = _parse_requirements_txt(project_root)

    all_deps: list[str] = []
    seen: set[str] = set()
    # De-duplicate by package name (lowercase, strip version specifiers)
    for spec in toml_deps + req_deps:
        name = re.split(r"[>=<!;\s\[]", spec)[0].strip().lower()
        if name and name not in seen:
            seen.add(name)
            all_deps.append(spec)

    if not all_deps:
        return []

    sources: list[str] = []
    if (project_root / "pyproject.toml").is_file():
        sources.append("pyproject.toml")
    if (project_root / "requirements.txt").is_file():
        sources.append("requirements.txt")
    source_str = " + ".join(sources) if sources else "pyproject.toml + requirements.txt"

    lines: list[str] = [f"### Python (from {source_str})", ""]
    for dep in all_deps:
        lines.append(f"- {dep}")
    lines.append("")
    return lines


def _build_external_nodejs(project_root: Path) -> list[str]:
    """Return markdown lines for Node.js external dependencies from package.json."""
    pkg_json = project_root / "package.json"
    if not pkg_json.is_file():
        return []

    text = _read_safe(pkg_json)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(data, dict):
        return []

    _node_header = (
        "### Node.js (from package.json — dependencies + devDependencies + peerDependencies)"
    )
    lines: list[str] = [_node_header, ""]
    has_any = False

    for key, label in (
        ("dependencies", ""),
        ("devDependencies", "dev"),
        ("peerDependencies", "peer"),
    ):
        section = data.get(key, {})
        if not isinstance(section, dict):
            continue
        for pkg_name, version in section.items():
            if label:
                lines.append(f"- {pkg_name} {version} ({label})")
            else:
                lines.append(f"- {pkg_name} {version}")
            has_any = True

    if not has_any:
        return []

    lines.append("")
    return lines


def _safe_parse_pom_text(text: str, pom: Path) -> Element | None:
    """Wrap :func:`parse_pom` so size violations log and degrade to None."""
    try:
        return parse_pom(text)
    except XmlSafetyError as exc:
        _logger.warning("Skipping %s: %s", pom, exc)
        return None


def _build_external_java(project_root: Path) -> list[str]:
    """Return markdown lines for Java external dependencies from pom.xml."""
    pom = project_root / "pom.xml"
    if not pom.is_file():
        return []

    text = _read_safe(pom)
    if not text:
        return []

    root = _safe_parse_pom_text(text, pom)
    if root is None:
        return []

    # Handle both namespaced and non-namespaced pom.xml
    ns = _MVN_NS if root.tag.startswith(f"{{{_MVN_NS}}}") else ""
    prefix = f"{{{ns}}}" if ns else ""

    # Find project-level groupId to filter out self-references
    project_group = ""
    project_artifact = ""
    g_el = root.find(f"{prefix}groupId")
    a_el = root.find(f"{prefix}artifactId")
    if g_el is not None and g_el.text:
        project_group = g_el.text.strip()
    if a_el is not None and a_el.text:
        project_artifact = a_el.text.strip()

    dep_entries: list[str] = []
    deps_el = root.find(f"{prefix}dependencies")
    if deps_el is not None:
        for dep in deps_el.findall(f"{prefix}dependency"):
            g = dep.find(f"{prefix}groupId")
            a = dep.find(f"{prefix}artifactId")
            v = dep.find(f"{prefix}version")
            if g is None or a is None:
                continue
            g_text = (g.text or "").strip()
            a_text = (a.text or "").strip()
            v_text = (v.text or "").strip() if v is not None else ""
            # Skip self-reference
            if g_text == project_group and a_text == project_artifact:
                continue
            if v_text:
                dep_entries.append(f"- {g_text}:{a_text} {v_text}")
            else:
                dep_entries.append(f"- {g_text}:{a_text}")

    if not dep_entries:
        return []

    lines: list[str] = ["### Java (from pom.xml)", ""]
    lines.extend(dep_entries)
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_dependency_map(
    project_root: Path,
    *,
    config: DigestConfig,
    include_vendored: bool = False,
) -> str:
    """Generate dependency-map.md content (markdown string).

    Sections produced (only when non-empty):
        # Dependency map
        ## Internal modules (top edges)
        ### Python
        ### TypeScript / JavaScript
        ### Java
        ## External dependencies
        ### Python (from pyproject.toml + requirements.txt)
        ### Node.js (from package.json — dependencies + devDependencies + peerDependencies)
        ### Java (from pom.xml)

    Empty sections are omitted entirely. Honors
    config.budget.per_file.dependency_map token cap (uses enforce_budget).
    """
    lines: list[str] = ["# Dependency map", ""]

    # --- Internal modules ---
    internal_py = _build_internal_python(project_root, include_vendored=include_vendored)
    internal_ts = _build_internal_ts(project_root, include_vendored=include_vendored)
    internal_java = _build_internal_java(project_root, include_vendored=include_vendored)

    if internal_py or internal_ts or internal_java:
        lines.append("## Internal modules (top edges)")
        lines.append("")
        lines.extend(internal_py)
        lines.extend(internal_ts)
        lines.extend(internal_java)

    # --- External dependencies ---
    ext_py = _build_external_python(project_root)
    ext_node = _build_external_nodejs(project_root)
    ext_java = _build_external_java(project_root)

    if ext_py or ext_node or ext_java:
        lines.append("## External dependencies")
        lines.append("")
        lines.extend(ext_py)
        lines.extend(ext_node)
        lines.extend(ext_java)

    if len(lines) == _HEADING_ONLY_LINES:
        # Only the heading and blank line — no data found
        lines.append("_No dependencies detected._")
        lines.append("")

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.dependency_map)

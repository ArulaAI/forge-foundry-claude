"""API-surface extractor — generates api-surface.md content.

Produces a structured markdown string covering:
  - Exported Python symbols (defs + classes) from top-ranked files
  - Exported TypeScript / JavaScript symbols
  - Public Java classes and methods
  - OpenAPI spec detection (paths + operations count)
  - CLI entrypoint detection (Click / argparse)

Output is truncated to config.budget.per_file.api_surface tokens via enforce_budget.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from primitives._shared.scan_exclusions import walk_source_files as _walk_source_files
from primitives.repo_digest.shared.budget import enforce_budget
from primitives.repo_digest.shared.ranking import rank_files

if TYPE_CHECKING:
    from primitives.repo_digest.shared.config import DigestConfig

_TOP_SYMBOLS = 20  # max exported symbols per language

# ---------------------------------------------------------------------------
# Source-file collection
# ---------------------------------------------------------------------------


def _collect_files(
    project_root: Path,
    extensions: set[str],
    *,
    include_vendored: bool = False,
) -> list[Path]:
    """Walk project_root and return files matching extensions, skipping excluded dirs."""
    return list(_walk_source_files(project_root, extensions, include_vendored=include_vendored))


def _read_safe(path: Path) -> str:
    """Read file text; return empty string on decode/permission error."""
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# Python symbol extraction
# ---------------------------------------------------------------------------

# Top-level function: "def name(" with no leading whitespace
_PY_FUNC_RE = re.compile(r"^def\s+([a-zA-Z_]\w*)\s*\(([^)]*)\)", re.MULTILINE)
# Top-level class: "class Name" with no leading whitespace
_PY_CLASS_RE = re.compile(r"^class\s+([A-Z_][a-zA-Z0-9_]*)\s*[:\(]", re.MULTILINE)


def _extract_python_symbols(
    content: str,
    rel_path: str,
    *,
    include_private: bool,
) -> list[str]:
    """Return formatted symbol strings for a single Python file."""
    symbols: list[str] = []
    # Classes first
    for m in _PY_CLASS_RE.finditer(content):
        name = m.group(1)
        if not include_private and name.startswith("_"):
            continue
        # Derive module-style name from path: src/foo/bar.py -> foo.bar
        mod = _module_from_path(rel_path)
        symbols.append(f"`{mod}.{name}` — from {rel_path}")
    # Then functions
    for m in _PY_FUNC_RE.finditer(content):
        name = m.group(1)
        if not include_private and name.startswith("_"):
            continue
        args = m.group(2).strip()
        mod = _module_from_path(rel_path)
        symbols.append(f"`{mod}.{name}({args})` — from {rel_path}")
    return symbols


def _module_from_path(rel_path: str) -> str:
    """Convert a relative file path to a dotted module reference.

    E.g. "src/foo/bar.py" -> "foo.bar", "module.py" -> "module".
    Strips leading "src/" or "lib/" prefix if present.
    """
    p = Path(rel_path)
    parts = list(p.with_suffix("").parts)
    # Strip common source root prefixes
    if parts and parts[0] in ("src", "lib", "app"):
        parts = parts[1:]
    return ".".join(parts) if parts else p.stem


# ---------------------------------------------------------------------------
# TypeScript / JavaScript symbol extraction
# ---------------------------------------------------------------------------

# export [async] function|class|const|let|var|interface|type Name
_TS_EXPORT_RE = re.compile(
    r"^export\s+(?:async\s+)?(?:default\s+)?"
    r"(function|class|const|let|var|interface|type)\s+([a-zA-Z_$][\w$]*)",
    re.MULTILINE,
)
# Also catch: export { Foo, Bar } and export default function ...
_TS_EXPORT_BARE_RE = re.compile(
    r"^export\s+(?:async\s+)?(function|class)\b",
    re.MULTILINE,
)


def _extract_ts_symbols(content: str, rel_path: str) -> list[str]:
    """Return formatted symbol strings for a TypeScript/JavaScript file."""
    symbols: list[str] = []
    seen: set[str] = set()
    for m in _TS_EXPORT_RE.finditer(content):
        keyword = m.group(1)
        name = m.group(2)
        entry = f"export {keyword} {name}"
        if entry not in seen:
            seen.add(entry)
            symbols.append(f"`{entry}` — from {rel_path}")
    return symbols


# ---------------------------------------------------------------------------
# Java symbol extraction
# ---------------------------------------------------------------------------

# public [abstract|final] class|interface|enum Name
_JAVA_TYPE_RE = re.compile(
    r"^\s*public\s+(?:abstract\s+|final\s+|static\s+)*"
    r"(class|interface|enum)\s+([A-Z][a-zA-Z0-9_]*)",
    re.MULTILINE,
)
# public [static] [final] ReturnType methodName(
_JAVA_METHOD_RE = re.compile(
    r"^\s*public\s+(?:static\s+|final\s+|abstract\s+|synchronized\s+)*"
    r"(?:\w+(?:<[^>]*>)?\s+)+"
    r"([a-z][a-zA-Z0-9_]*)\s*\(",
    re.MULTILINE,
)


def _extract_java_symbols(content: str, rel_path: str) -> list[str]:
    """Return formatted symbol strings for a Java file."""
    symbols: list[str] = []
    class_name: str | None = None

    for m in _JAVA_TYPE_RE.finditer(content):
        keyword = m.group(1)
        name = m.group(2)
        if class_name is None:
            class_name = name
        symbols.append(f"`public {keyword} {name}` — from {rel_path}")

    prefix = f"{class_name}." if class_name else ""
    for m in _JAVA_METHOD_RE.finditer(content):
        method = m.group(1)
        if method in ("class", "interface", "enum", "new", "return", "throw", "if", "while"):
            continue
        symbols.append(f"`public {prefix}{method}()` — from {rel_path}")

    return symbols


# ---------------------------------------------------------------------------
# OpenAPI detection
# ---------------------------------------------------------------------------

_OPENAPI_CANDIDATES = [
    "openapi.yaml",
    "openapi.json",
    "swagger.yaml",
    "swagger.json",
]
_OPENAPI_SUBDIRS = ["", "docs", "api", "spec", "specs"]


def _find_openapi_files(project_root: Path) -> list[Path]:
    """Search for OpenAPI/Swagger spec files at root and under common subdirs."""
    found: list[Path] = []
    for subdir in _OPENAPI_SUBDIRS:
        base = project_root / subdir if subdir else project_root
        for name in _OPENAPI_CANDIDATES:
            candidate = base / name
            if candidate.is_file():
                found.append(candidate)
    return found


def _parse_openapi(path: Path) -> tuple[int, int] | None:
    """Return (path_count, operation_count) for an OpenAPI spec, or None on error."""
    text = _read_safe(path)
    if not text:
        return None
    try:
        data = yaml.safe_load(text) if path.suffix in (".yaml", ".yml") else json.loads(text)
    except (yaml.YAMLError, json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        return None

    path_count = len(paths)
    op_count = 0
    http_methods = {"get", "post", "put", "patch", "delete", "options", "head", "trace"}
    for _path, path_item in paths.items():
        if isinstance(path_item, dict):
            op_count += sum(1 for k in path_item if k.lower() in http_methods)

    return path_count, op_count


def _build_openapi_section(project_root: Path) -> list[str]:
    """Build lines for the ## OpenAPI section."""
    lines: list[str] = ["## OpenAPI", ""]
    specs = _find_openapi_files(project_root)
    if not specs:
        lines.append("_No OpenAPI / Swagger spec detected._")
        lines.append("")
        return lines

    for spec_path in specs:
        result = _parse_openapi(spec_path)
        if result is None:
            continue
        path_count, op_count = result
        try:
            rel = spec_path.relative_to(project_root)
        except ValueError:
            rel = spec_path
        lines.append(f"- `{rel}`: {path_count} paths, {op_count} operations")

    _OPENAPI_HEADER_LINES = 2  # "## OpenAPI" + blank
    if len(lines) == _OPENAPI_HEADER_LINES:
        lines.append("_No parseable OpenAPI spec found._")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# CLI detection
# ---------------------------------------------------------------------------

_CLICK_CMD_RE = re.compile(r"@click\.(?:command|group)\s*\(", re.MULTILINE)
_CLICK_NAME_RE = re.compile(r"@click\.(?:command|group)\s*\([^)]*name\s*=\s*[\"']([^\"']+)[\"']")
_ARGPARSE_RE = re.compile(r"argparse\.ArgumentParser\s*\(", re.MULTILINE)

# Also look for Click decorated function names
_CLICK_FUNC_RE = re.compile(
    r"@click\.(?:command|group).*?\ndef\s+([a-zA-Z_]\w*)\s*\(",
    re.DOTALL,
)


def _build_cli_section(project_root: Path, *, include_vendored: bool = False) -> list[str]:
    """Build lines for the ## CLI commands section."""
    lines: list[str] = ["## CLI commands", ""]
    py_files = _collect_files(project_root, {".py"}, include_vendored=include_vendored)

    cli_entries: list[str] = []

    for f in py_files:
        content = _read_safe(f)
        if not content:
            continue

        try:
            rel = str(f.relative_to(project_root))
        except ValueError:
            rel = str(f)

        # Click commands
        click_matches = _CLICK_CMD_RE.findall(content)
        if click_matches:
            # Try to extract explicit name= argument
            for m in _CLICK_NAME_RE.finditer(content):
                cli_entries.append(f"- `{m.group(1)}` — Click command in {rel}")
            if not _CLICK_NAME_RE.search(content):
                # Fall back to decorated function names
                for m in _CLICK_FUNC_RE.finditer(content):
                    func_name = m.group(1).replace("_", "-")
                    cli_entries.append(f"- `{func_name}` — Click command in {rel}")
                if not _CLICK_FUNC_RE.search(content):
                    count = len(click_matches)
                    cli_entries.append(f"- {count} Click command(s) in {rel}")

        # argparse
        argparse_matches = _ARGPARSE_RE.findall(content)
        if argparse_matches:
            count = len(argparse_matches)
            cli_entries.append(f"- {count} argparse CLI entrypoint(s) in {rel}")

    if cli_entries:
        lines.extend(cli_entries)
    else:
        lines.append("_No Click / argparse CLI entrypoints detected._")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Language-section builders
# ---------------------------------------------------------------------------


def _build_python_section(
    project_root: Path,
    config: DigestConfig,
    *,
    include_vendored: bool = False,
) -> list[str]:
    """Build the ### Python subsection of exported symbols."""
    lines: list[str] = ["### Python", ""]
    py_files = _collect_files(project_root, {".py"}, include_vendored=include_vendored)
    if not py_files:
        lines.append("_No Python source files detected._")
        lines.append("")
        return lines

    ranked = rank_files(
        py_files,
        project_root=project_root,
        weights=config.ranking_weights,
        days=config.extractors.recent_deltas.days,
    )

    include_private = config.extractors.api_surface.include_private
    all_symbols: list[str] = []
    for f, _ in ranked:
        content = _read_safe(f)
        try:
            rel = str(f.relative_to(project_root))
        except ValueError:
            rel = str(f)
        syms = _extract_python_symbols(content, rel, include_private=include_private)
        all_symbols.extend(syms)
        if len(all_symbols) >= _TOP_SYMBOLS:
            break

    for sym in all_symbols[:_TOP_SYMBOLS]:
        lines.append(f"- {sym}")

    if not all_symbols:
        lines.append("_No public Python symbols found._")

    lines.append("")
    return lines


def _build_ts_section(
    project_root: Path,
    config: DigestConfig,
    *,
    include_vendored: bool = False,
) -> list[str]:
    """Build the ### TypeScript / JavaScript subsection of exported symbols."""
    lines: list[str] = ["### TypeScript / JavaScript", ""]
    ts_files = _collect_files(
        project_root,
        {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
        include_vendored=include_vendored,
    )
    if not ts_files:
        lines.append("_No TypeScript / JavaScript source files detected._")
        lines.append("")
        return lines

    ranked = rank_files(
        ts_files,
        project_root=project_root,
        weights=config.ranking_weights,
        days=config.extractors.recent_deltas.days,
    )

    all_symbols: list[str] = []
    for f, _ in ranked:
        content = _read_safe(f)
        try:
            rel = str(f.relative_to(project_root))
        except ValueError:
            rel = str(f)
        syms = _extract_ts_symbols(content, rel)
        all_symbols.extend(syms)
        if len(all_symbols) >= _TOP_SYMBOLS:
            break

    for sym in all_symbols[:_TOP_SYMBOLS]:
        lines.append(f"- {sym}")

    if not all_symbols:
        lines.append("_No TypeScript / JavaScript exports found._")

    lines.append("")
    return lines


def _build_java_section(
    project_root: Path,
    config: DigestConfig,
    *,
    include_vendored: bool = False,
) -> list[str]:
    """Build the ### Java subsection of exported symbols."""
    lines: list[str] = ["### Java", ""]
    java_files = _collect_files(project_root, {".java"}, include_vendored=include_vendored)
    if not java_files:
        lines.append("_No Java source files detected._")
        lines.append("")
        return lines

    ranked = rank_files(
        java_files,
        project_root=project_root,
        weights=config.ranking_weights,
        days=config.extractors.recent_deltas.days,
    )

    all_symbols: list[str] = []
    for f, _ in ranked:
        content = _read_safe(f)
        try:
            rel = str(f.relative_to(project_root))
        except ValueError:
            rel = str(f)
        syms = _extract_java_symbols(content, rel)
        all_symbols.extend(syms)
        if len(all_symbols) >= _TOP_SYMBOLS:
            break

    for sym in all_symbols[:_TOP_SYMBOLS]:
        lines.append(f"- {sym}")

    if not all_symbols:
        lines.append("_No public Java symbols found._")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_api_surface(
    project_root: Path,
    *,
    config: DigestConfig,
    include_vendored: bool = False,
) -> str:
    """Generate api-surface.md content.

    Sections produced:
        # API surface
        ## Exported symbols (top-ranked files)
        ### Python
        - `module.func(args)` — from path
        ### TypeScript / JavaScript
        - `export function foo` — from path
        ### Java
        - `public class Foo` — from path
        ## OpenAPI
        - `openapi.yaml`: N paths, M operations
        ## CLI commands
        - `command-name` — Click command in path

    Honors config.budget.per_file.api_surface token cap (uses enforce_budget).
    """
    lines: list[str] = [
        "# API surface",
        "",
        "## Exported symbols (top-ranked files)",
        "",
    ]

    lines.extend(_build_python_section(project_root, config, include_vendored=include_vendored))
    lines.extend(_build_ts_section(project_root, config, include_vendored=include_vendored))
    lines.extend(_build_java_section(project_root, config, include_vendored=include_vendored))
    lines.extend(_build_openapi_section(project_root))
    lines.extend(_build_cli_section(project_root, include_vendored=include_vendored))

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.api_surface)

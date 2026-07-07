"""Entry-points extractor — generates entry-points.md content.

Produces a structured markdown string covering:
  - Process entry points (Python __main__ guards, Java public static void main)
  - HTTP route handlers (FastAPI/Flask decorators, Express app.method(), Spring @*Mapping)
  - Lambda handlers (from serverless.yaml / SAM template / lambda_function.py)
  - pytest fixtures (top 5 by ranking)

Sections without any matches are omitted. Output is truncated to
config.budget.per_file.entry_points tokens via enforce_budget.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

import yaml

from primitives._shared.scan_exclusions import walk_source_files as _walk_source_files
from primitives.repo_digest.shared.budget import enforce_budget
from primitives.repo_digest.shared.ranking import rank_files

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import DigestConfig

_MAX_PYTEST_FIXTURES = 5

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Python __main__ guard
_PY_MAIN_RE = re.compile(r'if\s+__name__\s*==\s*["\']__main__["\']', re.MULTILINE)

# FastAPI: @app.get("/path") @router.post("/path") etc.
_FASTAPI_DECO_RE = re.compile(
    r'^(\s*)@(?:app|router)\.(get|post|put|delete|patch|head|options)\s*\(\s*["\']([^"\']*)["\']',
    re.MULTILINE,
)

# Flask: @app.route("/path", methods=...)
_FLASK_ROUTE_RE = re.compile(
    r'^(\s*)@(?:app|blueprint)\.(route)\s*\(\s*["\']([^"\']*)["\']',
    re.MULTILINE,
)

# Express.js: app.get("/path", ...) or router.post("/path", ...)
_EXPRESS_RE = re.compile(
    r'^(\s*)(?:app|router)\.(get|post|put|delete|patch|use)\s*\(\s*["\']([^"\']*)["\']',
    re.MULTILINE,
)

# Java public static void main(
_JAVA_MAIN_RE = re.compile(r"public\s+static\s+void\s+main\s*\(", re.MULTILINE)

# Spring route mappings
_SPRING_MAPPING_RE = re.compile(
    r"^(\s*)@(GetMapping|PostMapping|PutMapping|DeleteMapping|PatchMapping|RequestMapping)"
    r'\s*(?:\(\s*(?:value\s*=\s*)?["\']([^"\']*)["\'])?',
    re.MULTILINE,
)

# pytest fixture: @pytest.fixture followed by def name(
_PYTEST_FIXTURE_RE = re.compile(
    r"@pytest\.fixture[^\n]*\n\s*def\s+([a-zA-Z_]\w*)\s*\(",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# File collection
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


def _rel(f: Path, project_root: Path) -> str:
    """Return POSIX relative path string or absolute path string."""
    try:
        return f.relative_to(project_root).as_posix()
    except ValueError:
        return str(f)


def _line_number(content: str, match: re.Match) -> int:  # type: ignore[type-arg]
    """Return 1-based line number of a match start position."""
    return content[: match.start()].count("\n") + 1


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_process_entry_points(project_root: Path, *, include_vendored: bool = False) -> list[str]:
    """Detect Python __main__ guards and Java public static void main."""
    entries: list[str] = []

    # Python __main__
    py_files = _collect_files(project_root, {".py"}, include_vendored=include_vendored)
    for f in sorted(py_files):
        content = _read_safe(f)
        for m in _PY_MAIN_RE.finditer(content):
            lineno = _line_number(content, m)
            rel = _rel(f, project_root)
            entries.append(f'- `{rel}:{lineno}` — `if __name__ == "__main__":`')

    # Java public static void main
    java_files = _collect_files(project_root, {".java"}, include_vendored=include_vendored)
    for f in sorted(java_files):
        content = _read_safe(f)
        for m in _JAVA_MAIN_RE.finditer(content):
            lineno = _line_number(content, m)
            rel = _rel(f, project_root)
            entries.append(f"- `{rel}:{lineno}` — `public static void main`")

    return entries


def _build_http_handlers(project_root: Path, *, include_vendored: bool = False) -> list[str]:
    """Detect FastAPI, Flask, Express.js, and Spring HTTP route handlers."""
    entries: list[str] = []

    # Python: FastAPI + Flask
    py_files = _collect_files(project_root, {".py"}, include_vendored=include_vendored)
    for f in sorted(py_files):
        content = _read_safe(f)
        rel = _rel(f, project_root)

        # FastAPI decorators
        for m in _FASTAPI_DECO_RE.finditer(content):
            method = m.group(2).upper()
            path = m.group(3)
            lineno = _line_number(content, m)
            entries.append(f'- `{rel}:{lineno}` — `@app.{method.lower()}("{path}")` (FastAPI)')

        # Flask routes
        for m in _FLASK_ROUTE_RE.finditer(content):
            path = m.group(3)
            lineno = _line_number(content, m)
            entries.append(f'- `{rel}:{lineno}` — `@app.route("{path}", ...)` (Flask)')

    # TypeScript / JavaScript: Express
    ts_files = _collect_files(
        project_root,
        {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
        include_vendored=include_vendored,
    )
    for f in sorted(ts_files):
        content = _read_safe(f)
        rel = _rel(f, project_root)

        for m in _EXPRESS_RE.finditer(content):
            method = m.group(2)
            path = m.group(3)
            lineno = _line_number(content, m)
            entries.append(f'- `{rel}:{lineno}` — `app.{method}("{path}", ...)` (Express)')

    # Java: Spring @*Mapping
    java_files = _collect_files(project_root, {".java"}, include_vendored=include_vendored)
    for f in sorted(java_files):
        content = _read_safe(f)
        rel = _rel(f, project_root)

        for m in _SPRING_MAPPING_RE.finditer(content):
            annotation = m.group(2)
            path = m.group(3) or ""
            lineno = _line_number(content, m)
            if path:
                entries.append(f'- `{rel}:{lineno}` — `@{annotation}("{path}")`')
            else:
                entries.append(f"- `{rel}:{lineno}` — `@{annotation}`")

    return entries


def _parse_serverless_yaml(path: Path, fname: str) -> list[str]:
    """Extract handler entries from a serverless.yaml/yml file."""
    entries: list[str] = []
    text = _read_safe(path)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return entries
    if not isinstance(data, dict):
        return entries
    functions = data.get("functions", {})
    if not isinstance(functions, dict):
        return entries
    for _fn_name, fn_cfg in functions.items():
        if isinstance(fn_cfg, dict) and "handler" in fn_cfg:
            entries.append(f"- `{fn_cfg['handler']}` (declared in {fname})")
    return entries


def _parse_sam_template(path: Path, fname: str) -> list[str]:
    """Extract Lambda handler entries from a SAM template.yaml/json file."""
    entries: list[str] = []
    text = _read_safe(path)
    try:
        data = json.loads(text) if fname.endswith(".json") else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError):  # handle both JSONDecodeError and YAMLError
        return entries
    if not isinstance(data, dict):
        return entries
    resources = data.get("Resources", {})
    if not isinstance(resources, dict):
        return entries
    for _res_name, res_cfg in resources.items():
        if not isinstance(res_cfg, dict):
            continue
        if res_cfg.get("Type") != "AWS::Serverless::Function":
            continue
        props = res_cfg.get("Properties", {})
        if isinstance(props, dict) and "Handler" in props:
            entries.append(f"- `{props['Handler']}` (declared in {fname})")
    return entries


_LAMBDA_HANDLER_RE = re.compile(r"^def\s+lambda_handler\s*\(", re.MULTILINE)


def _build_lambda_handlers(project_root: Path) -> list[str]:
    """Detect Lambda handlers from serverless.yaml, SAM template.yaml, or lambda_function.py."""
    entries: list[str] = []

    for fname in ("serverless.yaml", "serverless.yml"):
        p = project_root / fname
        if p.is_file():
            entries.extend(_parse_serverless_yaml(p, fname))

    for fname in ("template.yaml", "template.yml", "template.json"):
        p = project_root / fname
        if p.is_file():
            entries.extend(_parse_sam_template(p, fname))

    lambda_py = project_root / "lambda_function.py"
    if lambda_py.is_file():
        content = _read_safe(lambda_py)
        for m in _LAMBDA_HANDLER_RE.finditer(content):
            lineno = _line_number(content, m)
            entries.append(f"- `lambda_function.lambda_handler` (lambda_function.py:{lineno})")

    return entries


def _build_pytest_fixtures(
    project_root: Path, config: DigestConfig, *, include_vendored: bool = False
) -> list[str]:
    """Detect top-ranked pytest fixtures (up to _MAX_PYTEST_FIXTURES)."""
    py_files = _collect_files(project_root, {".py"}, include_vendored=include_vendored)
    if not py_files:
        return []

    ranked = rank_files(
        py_files,
        project_root=project_root,
        weights=config.ranking_weights,
        days=config.extractors.recent_deltas.days,
    )

    entries: list[str] = []
    seen_count = 0

    for f, _ in ranked:
        if seen_count >= _MAX_PYTEST_FIXTURES:
            break
        content = _read_safe(f)
        rel = _rel(f, project_root)

        for m in _PYTEST_FIXTURE_RE.finditer(content):
            if seen_count >= _MAX_PYTEST_FIXTURES:
                break
            name = m.group(1)
            lineno = _line_number(content, m)
            entries.append(f"- `{rel}:{lineno}` — `{name}`")
            seen_count += 1

    return entries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_entry_points(
    project_root: Path,
    *,
    config: DigestConfig,
    include_vendored: bool = False,
) -> str:
    """Generate entry-points.md content (markdown string).

    Sections produced (only when non-empty):
        # Entry points
        ## Process entry points
        ## HTTP route handlers
        ## Lambda handlers
        ## pytest fixtures (top 5 by ranking)

    Empty sections are omitted entirely. Honors config.budget.per_file.entry_points
    token cap (uses enforce_budget).
    """
    lines: list[str] = ["# Entry points", ""]

    process_entries = _build_process_entry_points(project_root, include_vendored=include_vendored)
    if process_entries:
        lines.append("## Process entry points")
        lines.append("")
        lines.extend(process_entries)
        lines.append("")

    http_entries = _build_http_handlers(project_root, include_vendored=include_vendored)
    if http_entries:
        lines.append("## HTTP route handlers")
        lines.append("")
        lines.extend(http_entries)
        lines.append("")

    lambda_entries = _build_lambda_handlers(project_root)
    if lambda_entries:
        lines.append("## Lambda handlers")
        lines.append("")
        lines.extend(lambda_entries)
        lines.append("")

    fixture_entries = _build_pytest_fixtures(
        project_root, config, include_vendored=include_vendored
    )
    if fixture_entries:
        lines.append("## pytest fixtures (top 5 by ranking)")
        lines.append("")
        lines.extend(fixture_entries)
        lines.append("")

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.entry_points)

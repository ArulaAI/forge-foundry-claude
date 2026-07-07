"""Regex-based JS/TS dependency graph walker.

find_tests_reaching_nodejs(source_file, project_root) returns the set of
JS/TS test files under project_root that transitively import source_file.
"""

from __future__ import annotations

import re
from pathlib import Path

_IMPORT_OR_REQUIRE = re.compile(
    r"""(?:import|export)\s+(?:[^'"]*?\sfrom\s+)?['"]([^'"]+)['"]|require\s*\(\s*['"]([^'"]+)['"]"""
)
_TEST_FILENAME = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs)$")
_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_SKIP_DIRS = frozenset({"node_modules", ".git", "dist", "build", ".next", ".turbo"})


def _is_test_file(p: Path) -> bool:
    if _TEST_FILENAME.search(p.name):
        return True
    return any(part in ("tests", "__tests__") for part in p.parts)


def _resolve(relative_path: str, importer_dir: Path) -> Path | None:
    """Resolve a relative import path to an actual file on disk."""
    if not relative_path.startswith("."):
        return None
    base = (importer_dir / relative_path).resolve()
    # Try direct file with any extension (when relative_path has no extension)
    for ext in _EXTENSIONS:
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            return candidate
    # Try replacing extension (when relative_path already carries one)
    for ext in _EXTENSIONS:
        candidate = base.with_suffix(ext)
        if candidate.is_file():
            return candidate
    # Try base/index.ext (directory import)
    if base.is_dir():
        for ext in _EXTENSIONS:
            candidate = base / f"index{ext}"
            if candidate.is_file():
                return candidate
    return None


def _extract_imports(file: Path) -> list[str]:
    """Return list of raw import/require path strings found in *file*."""
    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return []
    paths: list[str] = []
    for match in _IMPORT_OR_REQUIRE.finditer(content):
        path = match.group(1) or match.group(2)
        if path:
            paths.append(path)
    return paths


def find_tests_reaching_nodejs(
    source_file: Path,
    project_root: Path,
) -> set[Path]:
    """Return test files under project_root that transitively import source_file.

    Walks all *.ts, *.tsx, *.js, *.jsx, *.mjs, *.cjs files under project_root.
    Resolves relative imports ('./foo', '../bar/x') to file paths.
    Builds forward import graph; returns test files (*.test.*, *.spec.*,
    in tests/ or __tests__/ dirs) that transitively import source_file.
    Excludes node_modules/, .git/, dist/, build/, .next/, .turbo/.
    """
    project_root = project_root.resolve()
    source_file = source_file.resolve()

    all_files: list[Path] = []
    for p in project_root.rglob("*"):
        if not p.is_file():
            continue
        if not any(p.name.endswith(ext) for ext in _EXTENSIONS):
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        all_files.append(p)

    # Build forward graph: file → set of files it directly imports
    forward: dict[Path, set[Path]] = {}
    for f in all_files:
        importer_dir = f.parent
        targets: set[Path] = set()
        for rel in _extract_imports(f):
            resolved = _resolve(rel, importer_dir)
            if resolved is not None:
                targets.add(resolved)
        forward[f] = targets

    # BFS from each test file; collect those that reach source_file
    affected: set[Path] = set()
    for f in all_files:
        if not _is_test_file(f):
            continue
        visited: set[Path] = set()
        stack = list(forward.get(f, set()))
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            if cur == source_file:
                affected.add(f)
                break
            stack.extend(forward.get(cur, set()) - visited)
    return affected

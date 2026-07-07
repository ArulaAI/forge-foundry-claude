"""AST-based Python dependency graph walker.

find_tests_reaching_python(source_file, project_root) returns the set of
*.py test files under project_root that transitively import source_file.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

_SKIP_DIRS = {".venv", "__pycache__", "node_modules", ".git"}


def _module_name(file: Path, project_root: Path) -> str:
    """Convert project_root/src/foo/bar.py → 'src.foo.bar'."""
    rel = file.relative_to(project_root)
    parts = rel.with_suffix("").parts
    # Drop __init__ to allow 'src.foo' for src/foo/__init__.py
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _extract_imports(file: Path) -> set[str]:
    """Return set of top-level module names imported by *file*."""
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError, PermissionError):
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
            # Also record 'module.name' for each name (e.g., 'from src import foo'
            # → add 'src.foo' in addition to 'src', so sub-module imports resolve).
            for alias in node.names:
                imports.add(f"{node.module}.{alias.name}")
    return imports


def _is_test_file(file: Path) -> bool:
    return (
        "tests" in file.parts
        or "test" in file.parts
        or file.name.startswith("test_")
        or file.name.endswith("_test.py")
    )


def find_tests_reaching_python(
    source_file: Path,
    project_root: Path,
) -> set[Path]:
    """Return the set of *.py test files under project_root that transitively import *source_file*.

    Algorithm:
        1. Compute the dotted-module name of source_file relative to project_root
           (e.g., src/foo.py under project_root → 'src.foo').
        2. Walk every *.py file under project_root via rglob, parse AST.
        3. For each test file (path contains 'tests'/'test' dir, or basename starts
           with 'test_' or ends with '_test.py'), check if it (transitively) imports
           source_file.
        4. Transitivity: build full forward import graph of all .py files in project,
           then test reaches source_file iff source_file is reachable from test
           in the graph.

    For simplicity, this walker is conservative: an import 'src.foo' or 'from src.foo
    import bar' both count as importing src.foo.
    """
    project_root = project_root.resolve()
    source_file = source_file.resolve()

    # Collect all .py files + their import sets (skipping noise dirs)
    all_py: dict[Path, set[str]] = {}
    for dirpath, dirnames, filenames in os.walk(project_root):
        # Prune skip dirs in-place so os.walk doesn't descend into them
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fname in filenames:
            if fname.endswith(".py"):
                p = Path(dirpath) / fname
                all_py[p] = _extract_imports(p)

    # Build module → file mapping
    module_to_file: dict[str, Path] = {_module_name(p, project_root): p for p in all_py}

    # Forward graph: file → set of file paths it directly imports
    def _resolve_imports(imports: set[str]) -> set[Path]:
        reached: set[Path] = set()
        for mod in imports:
            if mod in module_to_file:
                reached.add(module_to_file[mod])
            else:
                # Try longest-prefix match for sub-module imports
                parts = mod.split(".")
                for i in range(len(parts) - 1, 0, -1):
                    parent = ".".join(parts[:i])
                    if parent in module_to_file:
                        reached.add(module_to_file[parent])
                        break
        return reached

    forward: dict[Path, set[Path]] = {f: _resolve_imports(imports) for f, imports in all_py.items()}

    # For each test file, BFS to see if it reaches source_file
    affected: set[Path] = set()
    for f in all_py:
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

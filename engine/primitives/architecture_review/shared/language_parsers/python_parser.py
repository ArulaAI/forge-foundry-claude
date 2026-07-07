"""C8 /architecture-review — Python AST-based dependency parser.

Extracts import edges from Python source files using ast.parse.

Resolution rules:
- `import x.y.z` produces edge to `x.y.z`
- `from x.y import z` produces edge to `x.y` (the module being imported from)
- `from x.y.z import foo` produces edge to `x.y.z`
- Syntax errors are skipped with a stderr warning.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def parse_python_file(file_path: Path) -> tuple[str, list[str]]:
    """Parse a Python file and return (module_name, list_of_imported_module_names).

    Args:
        file_path: Absolute path to the .py file.

    Returns:
        Tuple of (module_name, imports) where:
            module_name is derived from the file stem (e.g., 'web.views').
            imports is a list of module names this file imports.
    """
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        print(
            f"[architecture-review] WARNING: skipping {file_path}: {exc}",
            file=sys.stderr,
        )
        return file_path.stem, []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    return file_path.stem, imports


def extract_modules_and_edges(
    root: Path,
    py_files: list[Path],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Extract module names and dependency edges from a list of Python files.

    Args:
        root: Project root for computing dotted module names.
        py_files: List of Python source file paths.

    Returns:
        Tuple of:
            - modules: list of (module_name, file_path_str)
            - edges: list of (from_module_name, to_module_name)
    """
    file_to_module: dict[Path, str] = {}
    for f in py_files:
        try:
            rel = f.relative_to(root)
        except ValueError:
            rel = Path(f.name)
        parts = rel.with_suffix("").parts
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:
            file_to_module[f] = ".".join(parts)

    modules: list[tuple[str, str]] = [(mod_name, str(f)) for f, mod_name in file_to_module.items()]
    edges: list[tuple[str, str]] = []
    for f, mod_name in file_to_module.items():
        _, imports = parse_python_file(f)
        for imp in imports:
            edges.append((mod_name, imp))

    return modules, edges

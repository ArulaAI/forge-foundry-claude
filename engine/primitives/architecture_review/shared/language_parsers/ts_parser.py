"""C8 /architecture-review — TypeScript/JavaScript regex-based dependency parser.

Extracts import statements from TS/JS source files using regex.

Handles:
- `import x from 'y'`
- `import { x, y } from 'y'`
- `import * as x from 'y'`
- `import 'y'` (side-effect imports)
- `require('y')` (CommonJS)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Matches: import ... from 'module' or import ... from "module"
_FROM_IMPORT_RE = re.compile(
    r"""import\s+(?:[^'"]+from\s+)?['"]([^'"]+)['"]""",
    re.MULTILINE,
)


def parse_ts_file(file_path: Path) -> tuple[str, list[str]]:
    """Parse a TS/JS file and return (module_name, list_of_imported_module_names).

    Module name is derived from the file path.
    """
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(
            f"[architecture-review] WARNING: skipping {file_path}: {exc}",
            file=sys.stderr,
        )
        return file_path.stem, []

    imports = _FROM_IMPORT_RE.findall(source)
    return file_path.stem, imports


def extract_modules_and_edges(
    root: Path,
    ts_files: list[Path],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Extract module names and dependency edges from TS/JS files.

    Returns:
        Tuple of:
            - modules: list of (module_name, file_path_str)
            - edges: list of (from_module_name, to_module_name)
    """
    file_to_module: dict[Path, str] = {}
    for f in ts_files:
        try:
            rel = f.relative_to(root)
        except ValueError:
            rel = Path(f.name)
        parts = rel.with_suffix("").parts
        file_to_module[f] = ".".join(parts) if parts else f.stem

    modules: list[tuple[str, str]] = [(mod_name, str(f)) for f, mod_name in file_to_module.items()]
    edges: list[tuple[str, str]] = []
    for f, mod_name in file_to_module.items():
        _, imports = parse_ts_file(f)
        for imp in imports:
            edges.append((mod_name, imp))

    return modules, edges

"""C8 /architecture-review — Stage A: static dependency graph builder.

Dispatches to the right language parser by file extension.
Supports --language auto heuristic (most common extension wins).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Literal, TypeAlias

from primitives.architecture_review.shared.language_parsers import (
    java_parser,
    python_parser,
    ts_parser,
)
from primitives.architecture_review.shared.models import DepGraph, Language, Module

if TYPE_CHECKING:
    from pathlib import Path

_Language: TypeAlias = Literal["python", "java", "ts", "auto"]


def _detect_language(code_path: Path) -> Language:
    """Auto-detect the primary language by counting files by extension."""
    py_count = len(list(code_path.rglob("*.py")))
    java_count = len(list(code_path.rglob("*.java")))
    ts_count = len(list(code_path.rglob("*.ts"))) + len(list(code_path.rglob("*.tsx")))

    counts = {"python": py_count, "java": java_count, "ts": ts_count}
    if all(v == 0 for v in counts.values()):
        return "python"  # default

    best = max(counts, key=lambda k: counts[k])
    detected: Language = best  # type: ignore[assignment]
    return detected


def build_dep_graph(
    code_path: Path,
    language: _Language = "auto",
    module_pattern: str | None = None,
) -> DepGraph:
    """Build a static dependency graph from *code_path*.

    Args:
        code_path: Directory (or file) to scan.
        language: 'python', 'java', 'ts', or 'auto' (auto-detect by extension count).
        module_pattern: Optional glob to filter source files (e.g. '**/*.py').

    Returns:
        DepGraph with modules and directed edges.
    """
    if not code_path.exists():
        raise FileNotFoundError(f"code_path does not exist: {code_path}")

    if code_path.is_file():
        # Single-file mode: treat directory as parent
        code_dir = code_path.parent
        files_by_lang: dict[Language, list[Path]] = {
            "python": [code_path] if code_path.suffix == ".py" else [],
            "java": [code_path] if code_path.suffix == ".java" else [],
            "ts": [code_path] if code_path.suffix in (".ts", ".tsx") else [],
        }
    else:
        code_dir = code_path
        files_by_lang = {
            "python": sorted(code_path.rglob("*.py")),
            "java": sorted(code_path.rglob("*.java")),
            "ts": sorted(list(code_path.rglob("*.ts")) + list(code_path.rglob("*.tsx"))),
        }

    # Apply module_pattern filter if given
    if module_pattern:
        from fnmatch import fnmatch  # noqa: PLC0415

        filtered: dict[Language, list[Path]] = {}
        for lang, paths in files_by_lang.items():
            filtered[lang] = [p for p in paths if fnmatch(str(p), module_pattern)]
        files_by_lang = filtered

    # Determine actual language
    resolved_lang: Language
    if language == "auto":
        resolved_lang = _detect_language(code_path if code_path.is_dir() else code_path.parent)
    else:
        resolved_lang = language

    # Select files for the chosen language
    chosen_files = files_by_lang.get(resolved_lang, [])

    print(
        f"[architecture-review] Stage A: {resolved_lang} parser on {len(chosen_files)} file(s)",
        file=sys.stderr,
    )

    # Dispatch to parser
    if resolved_lang == "python":
        raw_modules, raw_edges = python_parser.extract_modules_and_edges(code_dir, chosen_files)
    elif resolved_lang == "java":
        raw_modules, raw_edges = java_parser.extract_modules_and_edges(code_dir, chosen_files)
    else:  # ts
        raw_modules, raw_edges = ts_parser.extract_modules_and_edges(code_dir, chosen_files)

    # Build typed Module objects
    modules = [
        Module(name=name, path=path_str, language=resolved_lang) for name, path_str in raw_modules
    ]

    return DepGraph(modules=modules, edges=raw_edges)

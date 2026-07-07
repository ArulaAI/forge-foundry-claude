"""Depgraph dispatcher — per-stack walker selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._sdet.depgraph.java import find_tests_reaching_java
from primitives._sdet.depgraph.nodejs import find_tests_reaching_nodejs
from primitives._sdet.depgraph.python import find_tests_reaching_python

if TYPE_CHECKING:
    from pathlib import Path

_PY = {".py"}
_NODE = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
_JAVA = {".java"}


def find_affected_tests(
    changed_files: list[Path],
    project_root: Path,
) -> dict[Path, set[Path]]:
    """Map each changed source file → set of test files it affects.

    Dispatches per file extension:
        .py  → find_tests_reaching_python
        .ts/.tsx/.js/.jsx/.mjs/.cjs → find_tests_reaching_nodejs
        .java → find_tests_reaching_java

    Files with unsupported extensions are silently skipped (returned dict has no
    entry for them).
    """
    result: dict[Path, set[Path]] = {}
    for f in changed_files:
        suffix = f.suffix
        if suffix in _PY:
            result[f] = find_tests_reaching_python(f, project_root)
        elif suffix in _NODE:
            result[f] = find_tests_reaching_nodejs(f, project_root)
        elif suffix in _JAVA:
            result[f] = find_tests_reaching_java(f, project_root)
        # else: silently skip (unsupported extension)
    return result


__all__ = ["find_affected_tests"]

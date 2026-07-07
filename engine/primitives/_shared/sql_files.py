"""Shared SQL file-walking utility (M1/M2 dedup, #816).

A single helper, :func:`walk_sql_files`, that was duplicated verbatim across the
M1 (``db_schema_analyze`` ``shared/ddl_parser.py``) and M2 (``stored_proc_analyze``
``shared/sp_parser.py``) Stage-A parsers.

It returns every ``*.sql`` file under ``root`` in deterministic (sorted) order,
short-circuiting single-file inputs to ``[root]`` regardless of suffix (the caller
explicitly pointed at the file). Symlinks are skipped so a directory traversal
cannot escape the supplied root via a crafted symlink.

Imports only stdlib, so it is import-safe under the QUAL-2 (#163) cross-primitive
guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def walk_sql_files(root: Path) -> list[Path]:
    """Return every ``*.sql`` file under ``root`` in deterministic order.

    Single-file inputs short-circuit to ``[root]`` regardless of suffix
    (the caller explicitly pointed at the file). Symlinks are skipped so a
    directory traversal cannot escape the supplied root via a crafted symlink.
    """
    if root.is_file():
        return [root]
    return sorted(p for p in root.rglob("*.sql") if p.is_file() and not p.is_symlink())

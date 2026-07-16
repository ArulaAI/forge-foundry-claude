"""Data-access extractor — surfaces the DB tables a repo reads/writes.

Produces a ``## Data Access`` markdown section listing every distinct database
table the codebase touches, one bullet per table::

    ## Data Access

    - `owners`
    - `pets`
    - `vets`

**Why this exists (the SP2↔SP3 join-key contract, #1405).** The
``mod-app-analysis`` (SP3) app-digest aggregator
(:mod:`primitives.app_analysis.shared.app_digest_aggregator`) parses the
``repo-digest`` output for a ``## Data Access`` section and emits each table as a
``data:<table>`` import edge.  The SP2 system-map correlator threads the
load-bearing UI→API→DB join **only** through those ``data:`` edges.  Before this
extractor existed, ``repo-digest`` emitted no data-access section at all, so an
unattended chain run recovered **zero** ``data:`` edges and silently severed the
API→DB half of the join.  This extractor closes that format gap: a fresh
``repo-digest`` now carries the section the aggregator needs.

Detection is heuristic and stack-neutral.  Signals:

* **JPA / Jakarta Persistence** — ``@Table(name = "owners")`` (explicit name),
  and ``@Entity`` class names as a fallback table name.
* **DDL** — ``CREATE TABLE [IF NOT EXISTS] [schema.]owners`` in ``*.sql`` files.
* **SQL string literals** — ``FROM owners`` / ``INSERT INTO owners`` /
  ``UPDATE owners`` / ``JOIN owners`` embedded in source (mappers, DAOs, inline
  SQL).  ``JOIN`` covers the many-to-many link tables (e.g. ``vet_specialties``)
  that never appear in a ``FROM`` clause.

Table names are normalized to lowercase and de-duplicated; the output is sorted
deterministically.  Output honors the ``repo_digest`` per-file budget.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from primitives._shared.scan_exclusions import walk_source_files as _walk_source_files
from primitives.repo_digest.shared.budget import enforce_budget

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import DigestConfig

# Source extensions worth scanning for DB access.
_SCAN_EXTENSIONS = {
    ".java",
    ".kt",
    ".py",
    ".sql",
    ".xml",
    ".rb",
    ".cs",
    ".php",
    ".go",
    ".ts",
    ".js",
}

# A plausible SQL identifier: optional schema prefix, then a table name.
_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_QUALIFIED = rf"(?:{_IDENT}\.)?({_IDENT})"

# JPA / Jakarta: @Table(name = "owners") — the explicit table name wins.
_JPA_TABLE_RE = re.compile(
    r"@(?:javax\.persistence\.|jakarta\.persistence\.)?Table\s*\(\s*"
    r"(?:[^)]*?\bname\s*=\s*)?[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']",
    re.IGNORECASE,
)
# @Entity class Foo — the class name is a fallback table name when @Table is absent.
_JPA_ENTITY_RE = re.compile(
    r"@(?:javax\.persistence\.|jakarta\.persistence\.)?Entity\b[^\n]*"
    r"(?:\n[^\n]*){0,4}?\bclass\s+([A-Z][A-Za-z0-9_]*)",
    re.MULTILINE,
)
# DDL: CREATE TABLE [IF NOT EXISTS] [schema.]owners
_DDL_CREATE_RE = re.compile(
    rf"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?{_QUALIFIED}[\"'`]?",
    re.IGNORECASE,
)
# SQL DML: FROM / INTO / UPDATE / JOIN <table>.  JOIN catches link tables.
_SQL_DML_RE = re.compile(
    rf"\b(?:FROM|INTO|UPDATE|JOIN)\s+[\"'`]?{_QUALIFIED}[\"'`]?",
    re.IGNORECASE,
)

# SQL keywords / noise that the DML regex can grab as a false "table".
_SQL_NOISE = frozenset(
    {
        "select",
        "where",
        "set",
        "values",
        "on",
        "as",
        "and",
        "or",
        "inner",
        "left",
        "right",
        "outer",
        "cross",
        "full",
        "join",
        "using",
        "dual",
    }
)

# CamelCase → snake_case for @Entity fallback names (OwnerRepository → owner).
_CAMEL_BOUNDARY_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, FileNotFoundError):
        return ""


def _entity_to_table(entity: str) -> str:
    """Best-effort JPA entity class name → table name (snake_case, lowercase)."""
    return _CAMEL_BOUNDARY_RE.sub("_", entity).lower()


def _extract_tables_from_text(text: str, suffix: str) -> set[str]:
    """Return the set of lowercased table names referenced in *text*."""
    tables: set[str] = set()

    # JPA @Table(name="...") — explicit names are authoritative.
    for m in _JPA_TABLE_RE.finditer(text):
        tables.add(m.group(1).lower())

    # @Entity fallback — only when the class carries no @Table on the same type.
    for m in _JPA_ENTITY_RE.finditer(text):
        entity = m.group(1)
        # Skip if this entity block also declares an explicit @Table (already captured).
        block = text[m.start() : m.start() + 400]
        if not _JPA_TABLE_RE.search(block):
            tables.add(_entity_to_table(entity))

    # DDL CREATE TABLE.
    for m in _DDL_CREATE_RE.finditer(text):
        tables.add(m.group(1).lower())

    # Embedded SQL DML — restrict to files likely to carry SQL to reduce noise.
    if suffix in {".sql", ".xml", ".java", ".kt", ".py", ".rb", ".cs", ".php", ".go"}:
        for m in _SQL_DML_RE.finditer(text):
            name = m.group(1).lower()
            if name not in _SQL_NOISE:
                tables.add(name)

    return tables


def extract_data_access(
    project_root: Path,
    *,
    config: DigestConfig,
    include_vendored: bool = False,
) -> str:
    """Generate the ``## Data Access`` markdown section (or an empty-marker section).

    The heading and bullet shape are the exact contract the SP3 app-digest
    aggregator's ``_parse_consumed_tables`` reads: ``## Data Access`` followed by
    ``- `<table>` `` bullets (see module docstring / #1405).

    Args:
        project_root: Project root to scan.
        config: DigestConfig (used only for the per-file budget cap).
        include_vendored: Forwarded to the source-file walk.

    Returns:
        A markdown string beginning with ``## Data Access``.  When no tables are
        detected, the section renders a ``_No database access detected._``
        placeholder so the heading is always present.
    """
    tables: set[str] = set()
    for f in _walk_source_files(project_root, _SCAN_EXTENSIONS, include_vendored=include_vendored):
        tables |= _extract_tables_from_text(_read_safe(f), f.suffix)

    lines: list[str] = ["## Data Access", ""]
    if tables:
        for table in sorted(tables):
            lines.append(f"- `{table}`")
    else:
        lines.append("_No database access detected._")
    lines.append("")

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.repo_digest)

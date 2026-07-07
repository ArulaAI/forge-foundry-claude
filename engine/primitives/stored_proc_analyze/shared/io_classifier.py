"""M2 Stage C — ``classify_io_shape`` (reads / writes / cursor-heavy / cross-DB-link).

Classifies each parsed SP into one of the :data:`IOShape` literals
defined in :mod:`primitives.stored_proc_analyze.shared.models`.

Decision order (priority, top to bottom — first match wins):

1. ``cross_db_link`` — any table reference reaches a remote database.
   Detected via Oracle ``@dblink`` suffix on table names or T-SQL /
   linked-server multi-part names (``server.db.schema.table`` populates
   the AST ``catalog`` slot). DB2 nicknames are not syntactically
   distinguishable from local tables; the M3 strategist receives the
   nickname list from the schema digest and folds that signal in
   downstream — Stage C cannot detect them alone.
2. ``cursor_heavy`` — the SP declares a cursor *and* drives it with at
   least one ``FETCH``. The "declare + drive" pair is the noise filter
   that stops a one-off ``DECLARE c CURSOR`` (never opened) from being
   classified as cursor-heavy. sqlglot 30.x does not lift cursor
   keywords into AST nodes consistently across dialects, so the
   declaration signal comes from a comment-stripped raw-body regex
   while the fetch signal comes from the AST ``Command`` walker.
3. DML present + at least one ``SELECT`` somewhere in the body →
   ``reads_writes``.
4. DML present, no ``SELECT`` → ``writes_only``.
5. ``SELECT`` present, no DML → ``reads_only``.
6. Nothing detected (extremely degenerate SPs or unparseable bodies)
   → ``reads_only`` (the safest classification for risk scoring; a
   "we don't know" answer would force the downstream scorer to skip
   the row).

Design spec §M2 Stage C (`docs/superpowers/specs/2026-05-22-data-modernization-design.md`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlglot import exp

if TYPE_CHECKING:
    from primitives.stored_proc_analyze.shared.models import IOShape
    from primitives.stored_proc_analyze.shared.sp_parser import ParsedSP

# ---------------------------------------------------------------------------
# Regex pack — cursor-declaration detection from raw body
# ---------------------------------------------------------------------------

# Matches the DB2 / ANSI / PL/SQL ``DECLARE <name> CURSOR`` form and
# the T-SQL ``DECLARE <name> CURSOR`` form. ``CURSOR`` must follow
# ``DECLARE`` on the same logical statement, so we allow any non-newline
# characters between them but cap at 200 chars to keep the regex linear.
# Case-insensitive. Comments are stripped before the scan so a
# ``-- DECLARE c CURSOR`` doesn't trigger a false positive.
_CURSOR_DECLARE_RE = re.compile(
    r"\bDECLARE\b[^\n;]{0,200}\bCURSOR\b",
    re.IGNORECASE,
)

# Block-comment stripper used by the cursor-declaration scan. Matches
# the StaticProfile module's identical pattern; duplicated here so the
# modules stay independent (each stage is independent up to F per
# SPEC.md "Pipeline stages" note).
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_ONLY_RE = re.compile(r"^\s*--")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def classify_io_shape(parsed_sp: ParsedSP) -> IOShape:
    """Return the :data:`IOShape` literal describing ``parsed_sp``.

    Unparseable SPs fall through to the regex-based cursor signal and
    a default of ``reads_only`` if no signal fires — keeping a uniform
    return contract so downstream code does not branch on
    ``parse_status``.
    """
    ast = parsed_sp.ast

    # Step 1 — cross-DB link wins above everything else; a remote
    # reference dominates risk-scoring even when other patterns are
    # present.
    if ast is not None and _has_cross_db_reference(ast):
        return "cross_db_link"

    # Step 2 — cursor-heavy gate requires *both* a declaration and a
    # fetch. The declaration scan reads the raw body (sqlglot does not
    # lift DECLARE CURSOR consistently); the fetch scan reads the AST
    # (FETCH falls back to ``Command`` reliably across dialects).
    has_cursor_decl = _has_cursor_declaration(parsed_sp.raw_body)
    has_fetch = ast is not None and _has_fetch(ast)
    if has_cursor_decl and has_fetch:
        return "cursor_heavy"

    # Step 3-5 — the read / write breakdown only runs when the AST is
    # available. Unparseable bodies cannot be reliably bucketed beyond
    # the cursor signal above, so they fall through to step 6.
    if ast is None:
        return "reads_only"

    has_dml = _has_dml(ast)
    has_select = _has_top_level_select(ast)
    if has_dml and has_select:
        return "reads_writes"
    if has_dml:
        return "writes_only"
    if has_select:
        return "reads_only"

    # Step 6 — degenerate body (no DML, no SELECT). Default to
    # ``reads_only`` for risk-score safety (it's the lowest-impact
    # classification).
    return "reads_only"


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _has_cross_db_reference(ast: exp.Expression) -> bool:
    """Return ``True`` if any :class:`exp.Table` reaches a remote database.

    Two signals:

    * Oracle ``@dblink`` syntax — sqlglot parses ``orders@remote_db``
      as a single ``Table.name`` containing ``@``, so the membership
      test is sufficient.
    * T-SQL / linked-server multi-part naming — the parser populates
      ``Table.catalog`` (and ``Table.db``) when the source uses
      ``server.db.schema.table``. A non-empty ``catalog`` is the
      reliable T1 signal; M-tier consumers know catalog-prefixed
      references are cross-database.
    """
    for table in ast.find_all(exp.Table):
        name = table.name
        if isinstance(name, str) and "@" in name:
            return True
        if table.args.get("catalog") is not None:
            return True
    return False


def _has_cursor_declaration(raw_body: str) -> bool:
    """Return ``True`` if the body contains a ``DECLARE ... CURSOR`` form."""
    if not raw_body:
        return False
    stripped_blocks = _BLOCK_COMMENT_RE.sub("", raw_body)
    cleaned = "\n".join(
        line for line in stripped_blocks.splitlines() if not _LINE_COMMENT_ONLY_RE.match(line)
    )
    return bool(_CURSOR_DECLARE_RE.search(cleaned))


def _has_fetch(ast: exp.Expression) -> bool:
    """Return ``True`` if the AST contains at least one ``FETCH`` Command.

    sqlglot 30.x parses ``FETCH NEXT FROM cursor`` / ``FETCH c1 INTO v``
    as a :class:`exp.Command` with ``name="FETCH"`` across all three
    dialects in the M-tier matrix, so a single check is dialect-portable.
    """
    return any(cmd.name == "FETCH" for cmd in ast.find_all(exp.Command))


def _has_dml(ast: exp.Expression) -> bool:
    """Return ``True`` if the AST contains any INSERT / UPDATE / DELETE / MERGE."""
    return any(True for _ in ast.find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge))


def _has_top_level_select(ast: exp.Expression) -> bool:
    """Return ``True`` if the AST has at least one statement-form ``SELECT``.

    The walker matches :class:`exp.Select`, which covers ``SELECT id
    FROM t`` (free-standing query inside a procedure body). Scalar
    subqueries inside an INSERT / UPDATE WHERE clause also count —
    those legitimately represent the SP "reading" data as part of a
    write. The simpler ``reads_writes`` classification therefore fires
    correctly for ``INSERT ... SELECT FROM`` and ``UPDATE ... WHERE
    EXISTS (SELECT ...)`` patterns.
    """
    return any(True for _ in ast.find_all(exp.Select))


__all__ = ["classify_io_shape"]

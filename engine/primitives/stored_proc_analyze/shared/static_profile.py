"""M2 Stage B — ``compute_static_profile`` (LOC, cyclomatic, touches, out-calls, txn statements).

Walks the AST produced by Stage A (:func:`sp_parser.parse_stored_procs`)
and computes a deterministic per-SP profile. The numbers feed Stage F
(risk scoring) and the M3 strategist's sequencer.

Metrics:

* ``loc`` — body lines of code (blank lines and SQL comments excluded).
* ``cyclomatic`` — McCabe-shaped complexity. Base 1 + 1 per decision
  node (``IfBlock``, ``WhileBlock``, ``Case`` expression, cursor-loop
  ``LOOP`` / ``FOR ... LOOP`` / ``FETCH`` commands).
* ``touched_tables`` — sorted list of distinct table names that appear
  anywhere in the AST — DML write targets, subquery reads, JOIN sources,
  EXISTS probes, and correlated subqueries are all included.
* ``table_touch_count`` — ``len(touched_tables)`` (cached on the
  dataclass so consumers don't recompute).
* ``out_calls`` — count of sub-procedure invocations (``EXEC`` /
  ``EXECUTE`` / ``CALL``). Cross-SP fan-out is what M3 uses to detect
  orchestration patterns.
* ``transaction_statements`` — count of explicit transaction-control
  statements (``COMMIT`` / ``ROLLBACK`` / ``SAVEPOINT``). Non-zero
  values signal SPs that own transaction boundaries — a migration
  hazard M3 / M4 must surface.

Unparseable SPs (``parse_status="unparseable"``) still receive a
profile: ``loc`` is computed from ``raw_body``; AST-derived metrics
default to zero. This keeps the digest shape stable even when sqlglot
fell back gracefully (issue #205 contract).

Design spec §M2 Stage B (`docs/superpowers/specs/2026-05-22-data-modernization-design.md`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlglot import exp

if TYPE_CHECKING:
    from primitives.stored_proc_analyze.shared.sp_parser import ParsedSP

# ---------------------------------------------------------------------------
# Command-name vocabularies
# ---------------------------------------------------------------------------

# sqlglot falls back to ``exp.Command`` for procedural constructs it
# does not have a dedicated node for. We classify by the ``Command.name``
# token (which is the uppercase keyword the parser ate). Using a set
# keeps the lookup O(1) and the membership test trivially readable.

# Sub-procedure invocations. ``EXEC`` is T-SQL; ``EXECUTE`` is the
# canonical SQL/PSM verb; ``CALL`` is the DB2 / ANSI verb plus the
# PL/SQL alias for stored-procedure invocation.
_OUT_CALL_COMMANDS: frozenset[str] = frozenset({"EXEC", "EXECUTE", "CALL"})

# Cursor-loop tokens. A cursor-driven loop contributes a decision-edge
# to cyclomatic complexity even when sqlglot does not lift it into a
# :class:`exp.WhileBlock`. ``FETCH`` is the universal cursor advance;
# ``LOOP`` is the explicit PL/SQL / DB2 SQL PL loop keyword.
_CURSOR_LOOP_COMMANDS: frozenset[str] = frozenset({"LOOP", "FETCH"})

# Lines that contain *only* a SQL comment (line-comment or block-comment
# delimiter) are not counted toward LOC. The regex matches a string that
# is whitespace + ``--`` … or whitespace + ``/*`` / ``*/`` boundaries.
# Block-comment interiors are handled by stripping ``/* ... */`` first.
_LINE_COMMENT_ONLY_RE = re.compile(r"^\s*--")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# sqlglot 30.x does not lift ``SAVEPOINT`` / ``RELEASE SAVEPOINT`` /
# ``ROLLBACK TO SAVEPOINT`` into dedicated nodes — they show up as a
# bare ``Column`` + ``Alias`` pair the AST walker cannot recognise as
# a transaction-control statement. We fall back to a raw-body regex
# scan for these tokens so the metric does not silently undercount
# legacy SPs that use savepoints for partial rollback. The regex is
# word-bounded and case-insensitive; comments are stripped before the
# scan so a savepoint mentioned inside ``-- TODO`` does not count.
_SAVEPOINT_RE = re.compile(r"\bSAVEPOINT\b", re.IGNORECASE)


# ---------------------------------------------------------------------------
# StaticProfile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StaticProfile:
    """Stage B output for a single :class:`ParsedSP`.

    Frozen so the value is hashable / cacheable; downstream stages (C
    IO-classifier, D side-effects, F risk-scorer) treat this as a
    read-only fact. The orchestrator (M2-10) folds the fields into the
    final :class:`StoredProc` Pydantic record.

    ``touched_tables`` is intentionally a tuple (not a list) so the
    dataclass stays hashable and downstream tests can do straight-up
    set equality against a fixture's expected target set.
    """

    loc: int
    cyclomatic: int
    table_touch_count: int
    touched_tables: tuple[str, ...]
    out_calls: int
    transaction_statements: int

    notes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_static_profile(parsed_sp: ParsedSP) -> StaticProfile:
    """Walk ``parsed_sp.ast`` and compute the per-SP static profile.

    Unparseable SPs still receive a profile with ``loc`` computed from
    the raw body and every AST-derived metric set to zero — the digest
    shape must be stable across parse-status so consumers can render
    a homogeneous table.
    """
    loc = _compute_loc(parsed_sp.raw_body)

    if parsed_sp.ast is None:
        return StaticProfile(
            loc=loc,
            cyclomatic=0,
            table_touch_count=0,
            touched_tables=(),
            out_calls=0,
            transaction_statements=0,
            notes=("ast unavailable; AST-derived metrics defaulted to 0",),
        )

    ast = parsed_sp.ast
    cyclomatic = _compute_cyclomatic(ast)
    touched = _compute_touched_tables(ast)
    out_calls = _compute_out_calls(ast)
    txn = _compute_transaction_statements(ast, parsed_sp.raw_body)

    return StaticProfile(
        loc=loc,
        cyclomatic=cyclomatic,
        table_touch_count=len(touched),
        touched_tables=touched,
        out_calls=out_calls,
        transaction_statements=txn,
    )


# ---------------------------------------------------------------------------
# LOC
# ---------------------------------------------------------------------------


def _compute_loc(raw_body: str) -> int:
    """Count non-blank, non-comment lines in ``raw_body``.

    Block comments (``/* ... */``) are stripped first so a comment-only
    region cannot inflate the count. Line comments (``-- ...``) are
    discarded line-by-line. The base LOC counter is intentionally
    permissive: a line with ``WHERE id = 1  -- inline note`` still
    counts (only the trailing comment is ignored conceptually; the
    code-bearing portion remains).
    """
    if not raw_body:
        return 0
    stripped_blocks = _BLOCK_COMMENT_RE.sub("", raw_body)
    count = 0
    for line in stripped_blocks.splitlines():
        if not line.strip():
            continue
        if _LINE_COMMENT_ONLY_RE.match(line):
            continue
        count += 1
    return count


# ---------------------------------------------------------------------------
# Cyclomatic complexity
# ---------------------------------------------------------------------------


def _compute_cyclomatic(ast: exp.Expression) -> int:
    """McCabe-shaped cyclomatic count. Base 1 + 1 per decision node.

    The decision-node set is intentionally narrow:

    * :class:`exp.IfBlock` — procedural IF blocks (TSQL / DB2 / Oracle).
      Each ELSEIF arm would also be a decision but sqlglot does not
      lift those into separate nodes, so they collapse into the parent
      IfBlock count. T1 accepts this conservative underestimate; M3
      / M4 use the relative ordering, not absolute calibration.
    * :class:`exp.WhileBlock` — procedural WHILE loops.
    * :class:`exp.Case` — SQL CASE expressions (one decision per CASE,
      regardless of WHEN-arm count, again conservative).
    * :class:`exp.Command` whose name appears in
      :data:`_CURSOR_LOOP_COMMANDS` — cursor-driven row-by-row work
      that sqlglot did not lift into a :class:`exp.WhileBlock`.
    """
    decisions = 0
    decisions += len(list(ast.find_all(exp.IfBlock)))
    decisions += len(list(ast.find_all(exp.WhileBlock)))
    decisions += len(list(ast.find_all(exp.Case)))
    for cmd in ast.find_all(exp.Command):
        if cmd.name in _CURSOR_LOOP_COMMANDS:
            decisions += 1
    return 1 + decisions


# ---------------------------------------------------------------------------
# Table touches (all AST references)
# ---------------------------------------------------------------------------

# sqlglot represents the CREATE PROCEDURE / CREATE FUNCTION definition
# header itself as a Table (Oracle) or StoredProcedure/UserDefinedFunction
# wrapping a Table (T-SQL / DB2). These parent types flag "definition
# context" — the procedure name, not a data-source reference.
_DEFINITION_PARENT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Create,
    exp.StoredProcedure,
    exp.UserDefinedFunction,
)

# Oracle's ``DUAL`` is a built-in one-row pseudo-table (the canonical
# ``SELECT ... FROM DUAL`` scaffold), not a schema object — and the same name
# qualified as ``SYS.DUAL``. Excluded from ``touched_tables`` so it does not
# inflate ``table_touch_count`` → the ``write_breadth`` risk axis (#950).
_PSEUDO_TABLE_NAMES: frozenset[str] = frozenset({"DUAL"})


def _compute_touched_tables(ast: exp.Expression) -> tuple[str, ...]:
    """Return the sorted set of distinct table names referenced anywhere in the AST.

    Captures all data sources visible to sqlglot — DML write targets
    (``INSERT``/``UPDATE``/``DELETE``/``MERGE``), ``FROM``/``JOIN``
    sources, ``EXISTS`` probes, correlated subqueries, and inline
    views — so ``touched_tables`` reflects the full data-dependency
    footprint of the procedure, not only its write side.

    Two categories of ``exp.Table`` nodes are excluded to avoid
    spurious entries:

    * **Procedure-definition nodes** — when sqlglot parses
      ``CREATE OR REPLACE PROCEDURE <name>`` (Oracle dialect) it
      attaches a ``Table`` whose parent is ``exp.Create``; T-SQL / DB2
      use ``exp.StoredProcedure`` or ``exp.UserDefinedFunction`` as the
      parent instead.  Neither is a data-source reference.
    * **CTE aliases** — a ``WITH cte_name AS (…)`` body is a derived
      table; the alias appears as an ``exp.Table`` in downstream
      references but is not a physical table.
    * **Pseudo-tables** — Oracle's ``DUAL`` (bare or ``SYS.DUAL``) is a
      built-in one-row scaffold, not a schema object (#950).

    Table names are emitted schema-prefixed when sqlglot recovered the
    schema (``dbo.Employees``, ``HR.EMPLOYEES``) so downstream M3
    bounded-context detection can disambiguate identically-named
    tables across schemas.
    """
    # Collect CTE alias names so they can be excluded below.
    cte_aliases: set[str] = {cte.alias for cte in ast.find_all(exp.CTE)}

    seen: set[str] = set()
    for table in ast.find_all(exp.Table):
        # Skip the procedure/function name itself.
        if isinstance(table.parent, _DEFINITION_PARENT_TYPES):
            continue
        # Skip CTE alias references — they are derived, not physical.
        if table.name in cte_aliases:
            continue
        # Skip built-in pseudo-tables (Oracle DUAL / SYS.DUAL) — not schema objects.
        if table.name.upper() in _PSEUDO_TABLE_NAMES:
            continue
        seen.add(_render_table_name(table))
    return tuple(sorted(seen))


def _render_table_name(table: exp.Table) -> str:
    """Render a :class:`exp.Table` as ``schema.name`` (or ``name``)."""
    db = table.args.get("db")
    schema_name = db.this if isinstance(db, exp.Identifier) else None
    table_name = table.name
    if schema_name:
        return f"{schema_name}.{table_name}"
    return table_name


# ---------------------------------------------------------------------------
# Out-calls (EXEC / EXECUTE / CALL)
# ---------------------------------------------------------------------------


def _compute_out_calls(ast: exp.Expression) -> int:
    """Count sub-procedure invocations.

    T-SQL ``EXEC``/``EXECUTE`` lifts to a dedicated :class:`exp.Execute`
    node, so those are counted directly. DB2 ``CALL`` and Oracle
    ``EXECUTE`` fall back to :class:`exp.Command` with a name in
    :data:`_OUT_CALL_COMMANDS`. ``EXECUTE IMMEDIATE`` (Oracle dynamic
    SQL) is *not* an out-call — Stage D classifies that as a
    ``dynamic_sql`` side effect — so we check the command expression
    for the ``IMMEDIATE`` token and exclude it here.
    """
    count = len(list(ast.find_all(exp.Execute)))
    for cmd in ast.find_all(exp.Command):
        if cmd.name not in _OUT_CALL_COMMANDS:
            continue
        if _is_execute_immediate(cmd):
            # EXECUTE IMMEDIATE — handled by Stage D, not here.
            continue
        count += 1
    return count


def _is_execute_immediate(cmd: exp.Command) -> bool:
    """Return ``True`` when ``cmd`` is an ``EXECUTE IMMEDIATE`` (Oracle
    dynamic SQL) rather than a sub-procedure invocation.

    sqlglot folds the SP body into ``Command(name="EXECUTE",
    expression=Literal("IMMEDIATE '<sql>'"))``, so we read the
    literal's underlying string (``Literal.this``) and look for the
    ``IMMEDIATE`` token. We *do not* use ``str(cmd.expression)`` because
    that round-trips the literal with surrounding quotes that would
    defeat the ``startswith`` check.
    """
    payload = cmd.expression
    if isinstance(payload, exp.Literal):
        value = payload.this
        if isinstance(value, str) and value.strip().upper().startswith("IMMEDIATE"):
            return True
    return False


# ---------------------------------------------------------------------------
# Transaction statements
# ---------------------------------------------------------------------------


def _compute_transaction_statements(ast: exp.Expression, raw_body: str) -> int:
    """Count COMMIT / ROLLBACK / SAVEPOINT (+ RELEASE SAVEPOINT) statements.

    ``COMMIT`` and ``ROLLBACK`` lift to dedicated AST nodes; ``RELEASE``
    sometimes appears as a Command. ``SAVEPOINT`` is the awkward case:
    sqlglot 30.x parses it as an unnamed column reference + alias,
    which the walker cannot recognise as a transaction-control
    statement. We fall back to a comment-stripped raw-body regex scan
    (see :data:`_SAVEPOINT_RE` rationale) so partial-rollback patterns
    are not silently dropped from the digest.
    """
    count = len(list(ast.find_all(exp.Commit)))
    count += len(list(ast.find_all(exp.Rollback)))
    # SAVEPOINT detection uses a raw-body regex because sqlglot 30.x
    # does not lift ``SAVEPOINT`` into a dedicated AST node — it parses
    # as a column/alias pair the walker cannot recognise. Strip comments
    # first so a savepoint mentioned in ``-- comment`` does not inflate.
    stripped_blocks = _BLOCK_COMMENT_RE.sub("", raw_body)
    cleaned = "\n".join(
        line for line in stripped_blocks.splitlines() if not _LINE_COMMENT_ONLY_RE.match(line)
    )
    count += len(_SAVEPOINT_RE.findall(cleaned))
    return count


__all__ = ["StaticProfile", "compute_static_profile"]

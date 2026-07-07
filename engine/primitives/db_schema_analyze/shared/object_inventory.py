"""M1 Stage B — ``build_inventory`` (walk sqlglot AST → Table/View/Sequence).

Consumes the flat list of :class:`sqlglot.exp.Expression` nodes produced
by Stage A (:mod:`shared.ddl_parser`) and projects every ``CREATE TABLE``,
``CREATE VIEW``, ``CREATE SEQUENCE``, ``CREATE INDEX`` and
``ALTER TABLE ... ADD CONSTRAINT`` statement into the M1 Pydantic models
defined in :mod:`shared.models`.

Design notes
------------

* Stage B is **deterministic** — the same AST list produces byte-identical
  Table/View/Sequence lists. LLM judgement is deferred to Stage D
  (``lob_classifier.py``).
* Stage E owns ``is_hotspot`` / ``is_orphan`` and Stage D owns
  ``lob_classification``; this module leaves those fields at their
  Pydantic defaults so downstream stages can populate them by
  re-constructing the Table with ``model_copy(update=...)``.
* ``row_count_estimate`` is left as ``None``. Only DDL with explicit
  ``ANALYZE`` stats (rare in dump output) would populate it; the
  hook lives here so a future enhancement (T1.5) can fill it without
  reshaping the API.
* ``CREATE INDEX`` and ``ALTER TABLE ADD CONSTRAINT`` produce side-effects
  on already-emitted tables: indexes are attached to their owning table
  and out-of-line FKs are appended to the table's ``foreign_keys`` list.
  A reference to a table that never appears in a ``CREATE TABLE`` is
  logged at WARNING and dropped — Stage A's parse-failure channel
  already handled the structural case.

Design spec §M1 Stage B
(``docs/superpowers/specs/2026-05-22-data-modernization-design.md``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlglot import exp

from primitives.db_schema_analyze.shared.models import (
    Column,
    ForeignKey,
    Index,
    IndexKind,
    OnDeleteAction,
    Sequence,
    Table,
    View,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_inventory(
    parsed_expressions: Iterable[exp.Expression],
) -> tuple[list[Table], list[View], list[Sequence]]:
    """Walk parsed AST nodes and emit ``(tables, views, sequences)``.

    The lists are returned in insertion (= file walk + textual) order so
    Stage C can build the dependency DAG without re-sorting, and so the
    JSON digest is byte-stable run-over-run.

    Args:
        parsed_expressions: Output of :func:`parse_ddl_files` — a flat
            iterable of top-level ``CREATE`` / ``ALTER`` AST nodes. The
            iterable may be consumed exactly once.

    Returns:
        ``(tables, views, sequences)``. Each element is a Pydantic-
        validated list, dialect-preserving on ``Column.type`` strings.
    """
    tables: list[Table] = []
    table_index: dict[str, int] = {}
    views: list[View] = []
    sequences: list[Sequence] = []
    deferred_alters: list[exp.Alter] = []
    deferred_indexes: list[exp.Create] = []

    for node in parsed_expressions:
        if isinstance(node, exp.Create):
            kind = (node.kind or "").upper()
            if kind == "TABLE":
                table = _build_table(node)
                if table is None:
                    continue
                qn = _qn(table.schema_, table.name)
                table_index[qn] = len(tables)
                tables.append(table)
            elif kind == "VIEW":
                view = _build_view(node)
                if view is not None:
                    views.append(view)
            elif kind == "SEQUENCE":
                sequence = _build_sequence(node)
                if sequence is not None:
                    sequences.append(sequence)
            elif kind in {"INDEX", "UNIQUE INDEX", "CLUSTERED INDEX", "NONCLUSTERED INDEX"}:
                # Defer attach until all tables seen — index statements
                # routinely appear after the CREATE TABLE block.
                deferred_indexes.append(node)
        elif isinstance(node, exp.Alter):
            deferred_alters.append(node)
        # Anything else (DROP, GRANT, COMMENT, Command-wrapped unparseable
        # syntax) is silently ignored. Stage A already logged unparseable
        # files; Stage B treats unknown nodes as "not its problem."

    # ALTER TABLE ADD CONSTRAINT FK and CREATE INDEX attach to already-
    # emitted tables. We mutate by rebuilding the Table (Pydantic models
    # are frozen-ish; the Table is mutable but its sub-lists are model-
    # frozen so we rebuild for clarity).
    for alter in deferred_alters:
        _apply_alter(alter, tables, table_index)
    for idx_create in deferred_indexes:
        _apply_index(idx_create, tables, table_index)

    return tables, views, sequences


# ---------------------------------------------------------------------------
# Qualified name helper
# ---------------------------------------------------------------------------


def _qn(schema: str, name: str) -> str:
    """Return ``schema.name`` if schema is set, otherwise just ``name``."""
    if schema:
        return f"{schema}.{name}"
    return name


# ---------------------------------------------------------------------------
# CREATE TABLE
# ---------------------------------------------------------------------------


def _build_table(node: exp.Create) -> Table | None:
    """Project a ``CREATE TABLE`` node into a :class:`Table` model.

    Returns ``None`` for malformed inputs (no schema body, no table
    identifier) — the caller skips silently because Stage A's parse
    channel already logged structural failures.
    """
    table_ref = _create_table_ref(node)
    if table_ref is None:
        return None
    schema_name, table_name = table_ref

    schema_def = node.this
    if not isinstance(schema_def, exp.Schema):
        # ``CREATE TABLE foo AS SELECT ...`` produces a non-Schema body.
        # We emit a column-less Table so the dependency DAG can still see
        # the name; downstream stages treat it as an opaque entity.
        return Table(
            name=table_name,
            schema=schema_name,
            columns=[],
        )

    columns: list[Column] = []
    primary_key: list[str] = []
    foreign_keys: list[ForeignKey] = []

    for child in schema_def.expressions:
        if isinstance(child, exp.ColumnDef):
            col, inline_fk, inline_pk = _build_column(child)
            columns.append(col)
            if inline_fk is not None:
                foreign_keys.append(inline_fk)
            if inline_pk:
                primary_key.append(col.name)
        elif isinstance(child, exp.Constraint):
            # Named table-level constraint — may carry PK, FK, or UNIQUE.
            for sub in child.expressions:
                cname = _identifier_name(child.this) if child.this is not None else ""
                _absorb_constraint(sub, cname, primary_key, foreign_keys)
        elif isinstance(child, exp.PrimaryKey):
            # Unnamed inline ``PRIMARY KEY (a, b)`` declaration.
            primary_key.extend(_columns_of(child.expressions))
        elif isinstance(child, exp.ForeignKey):
            # Unnamed inline table-level FK.
            fk = _foreign_key_from_ast(child, "")
            if fk is not None:
                foreign_keys.append(fk)

    return Table(
        name=table_name,
        schema=schema_name,
        columns=columns,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
    )


def _absorb_constraint(
    sub: exp.Expression,
    constraint_name: str,
    primary_key: list[str],
    foreign_keys: list[ForeignKey],
) -> None:
    """Apply a single sub-element of a named table-level ``CONSTRAINT``."""
    if isinstance(sub, exp.PrimaryKey):
        primary_key.extend(_columns_of(sub.expressions))
    elif isinstance(sub, exp.ForeignKey):
        fk = _foreign_key_from_ast(sub, constraint_name)
        if fk is not None:
            foreign_keys.append(fk)
    # UNIQUE / CHECK constraints are not tracked at Table level — UNIQUE
    # surfaces as an Index in T1 if the dialect emits CREATE UNIQUE INDEX.


def _build_column(node: exp.ColumnDef) -> tuple[Column, ForeignKey | None, bool]:
    """Project a ``ColumnDef`` into a :class:`Column`, picking up inline
    FK / inline PRIMARY KEY column constraints en route.

    Returns ``(column, inline_fk_or_none, is_inline_pk)``. The inline FK
    is named after the column when it appears unnamed inline.
    """
    column_name = _identifier_name(node.this)
    type_node = node.args.get("kind")
    type_sql = type_node.sql() if type_node is not None else "UNKNOWN"

    nullable = True
    default: str | None = None
    inline_fk: ForeignKey | None = None
    inline_pk = False

    for cstr in node.args.get("constraints") or []:
        kind = cstr.kind if isinstance(cstr, exp.ColumnConstraint) else None
        if isinstance(kind, exp.NotNullColumnConstraint):
            nullable = False
        elif isinstance(kind, exp.PrimaryKeyColumnConstraint):
            inline_pk = True
            nullable = False  # PK columns are implicitly NOT NULL.
        elif isinstance(kind, exp.DefaultColumnConstraint):
            inner = kind.this
            default = inner.sql() if inner is not None and hasattr(inner, "sql") else str(inner)
        elif isinstance(kind, exp.Reference):
            inline_fk = _foreign_key_from_reference(
                kind,
                constraint_name=f"fk_{column_name}",
                local_columns=[column_name],
            )

    column = Column(
        name=column_name,
        type=type_sql,
        nullable=nullable,
        default=default,
        pii_classification=None,  # S60 cross-reference owns this; deferred.
    )
    return column, inline_fk, inline_pk


# ---------------------------------------------------------------------------
# FOREIGN KEY extraction
# ---------------------------------------------------------------------------


def _foreign_key_from_ast(
    fk_node: exp.ForeignKey,
    constraint_name: str,
) -> ForeignKey | None:
    """Build a :class:`ForeignKey` from a table-level FK AST node.

    Returns ``None`` if the FK is structurally malformed (no reference).
    """
    local_cols = _columns_of(fk_node.expressions)
    ref = fk_node.args.get("reference")
    if not isinstance(ref, exp.Reference):
        return None
    return _build_foreign_key(
        ref,
        constraint_name=constraint_name or "",
        local_columns=local_cols,
    )


def _foreign_key_from_reference(
    ref: exp.Reference,
    constraint_name: str,
    local_columns: list[str],
) -> ForeignKey | None:
    """Same as :func:`_foreign_key_from_ast` but for an inline column FK.

    Inline FKs always carry exactly one local column, plus the referenced
    table and (single) referenced column inside the ``Reference``.
    """
    return _build_foreign_key(
        ref,
        constraint_name=constraint_name,
        local_columns=local_columns,
    )


def _build_foreign_key(
    ref: exp.Reference,
    constraint_name: str,
    local_columns: list[str],
) -> ForeignKey | None:
    """Common FK-builder shared by inline + table-level FK paths."""
    inner = ref.this
    if isinstance(inner, exp.Schema):
        ref_table = _identifier_name(inner.this)
        ref_cols = _columns_of(inner.expressions)
    elif isinstance(inner, exp.Table):
        ref_table = inner.name
        ref_cols = []
    else:
        return None

    on_delete = _extract_on_delete(ref.args.get("options") or [])

    return ForeignKey(
        constraint_name=constraint_name,
        columns=list(local_columns),
        referenced_table=ref_table,
        referenced_columns=ref_cols,
        on_delete=on_delete,
    )


def _extract_on_delete(options: list[object]) -> OnDeleteAction | None:
    """Project ``options`` strings (e.g. ``'ON DELETE CASCADE'``) onto the
    :data:`OnDeleteAction` literal alias.

    sqlglot 30.x carries FK options as raw strings on
    ``Reference.args['options']`` (see the ddl_parser scratch run). We
    normalise the recognised set and return ``None`` for anything else
    so the digest only carries valid Literal values.
    """
    mapping: dict[str, OnDeleteAction] = {
        "ON DELETE CASCADE": "cascade",
        "ON DELETE SET NULL": "set_null",
        "ON DELETE RESTRICT": "restrict",
        "ON DELETE NO ACTION": "no_action",
    }
    for raw in options:
        if isinstance(raw, str):
            normalised = raw.upper().strip()
            if normalised in mapping:
                return mapping[normalised]
    return None


# ---------------------------------------------------------------------------
# CREATE VIEW
# ---------------------------------------------------------------------------


def _build_view(node: exp.Create) -> View | None:
    """Project a ``CREATE VIEW`` (or ``CREATE OR REPLACE VIEW``) node."""
    table_ref = _create_table_ref(node)
    if table_ref is None:
        return None
    schema_name, view_name = table_ref

    body = node.expression
    if body is None:
        # ``CREATE VIEW v;`` with no SELECT clause — unusable; skip.
        return None

    definition_sql = body.sql()
    referenced_tables = _referenced_tables(body)

    return View(
        name=view_name,
        schema=schema_name,
        definition_sql=definition_sql,
        referenced_tables=referenced_tables,
    )


def _referenced_tables(body: exp.Expression) -> list[str]:
    """Return the de-duplicated, qualified-name list of tables referenced
    by the view body.

    Order preserves first-occurrence in the AST walk so the digest is
    deterministic.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for t in body.find_all(exp.Table):
        # Skip alias references (the AST emits both the underlying Table
        # node and any aliases; only the underlying carries a real name).
        if not t.name:
            continue
        qn = _qn(_table_schema(t), t.name)
        if qn in seen_set:
            continue
        seen_set.add(qn)
        seen.append(qn)
    return seen


# ---------------------------------------------------------------------------
# CREATE SEQUENCE
# ---------------------------------------------------------------------------


def _build_sequence(node: exp.Create) -> Sequence | None:
    """Project a ``CREATE SEQUENCE`` node into a :class:`Sequence`."""
    table_ref = _create_table_ref(node)
    if table_ref is None:
        return None
    schema_name, seq_name = table_ref

    start = 1
    increment = 1
    cache_size: int | None = None

    properties = node.args.get("properties")
    if isinstance(properties, exp.Properties):
        for prop in properties.expressions:
            if isinstance(prop, exp.SequenceProperties):
                raw_start = prop.args.get("start")
                raw_increment = prop.args.get("increment")
                raw_cache = prop.args.get("cache")
                start = _to_int(raw_start, fallback=start)
                increment = _to_int(raw_increment, fallback=increment)
                # sqlglot can emit ``NOCACHE`` as ``cache=None`` vs a
                # number for ``CACHE 20``; treat the absence as None.
                if raw_cache is not None:
                    cache_size = _to_int(raw_cache, fallback=0) or None

    return Sequence(
        name=seq_name,
        schema=schema_name,
        start=start,
        increment=increment,
        cache_size=cache_size,
    )


def _to_int(raw: object, fallback: int) -> int:
    """Best-effort coerce sqlglot's various int-shaped expressions to int.

    sqlglot returns sequence properties as either bare Python ``int`` or
    a :class:`~sqlglot.exp.Literal` wrapping the digits. Anything else
    falls back to the caller's default so the model still validates.
    """
    if isinstance(raw, int):
        return raw
    if isinstance(raw, exp.Literal):
        try:
            return int(raw.this)
        except (TypeError, ValueError):
            return fallback
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError:
            return fallback
    return fallback


# ---------------------------------------------------------------------------
# ALTER TABLE ADD CONSTRAINT (deferred FK attachment)
# ---------------------------------------------------------------------------


def _apply_alter(
    node: exp.Alter,
    tables: list[Table],
    table_index: dict[str, int],
) -> None:
    """Apply a single ``ALTER TABLE`` to an already-emitted Table.

    T1 only handles ``ADD CONSTRAINT FOREIGN KEY``. Other ALTER actions
    (DROP, RENAME, MODIFY COLUMN) are silently skipped — the digest is a
    snapshot, not a migration log.
    """
    target = node.this
    if not isinstance(target, exp.Table):
        return
    target_qn = _qn(_table_schema(target), target.name)
    table_idx = table_index.get(target_qn)
    if table_idx is None:
        # ALTER targeting a table we never saw a CREATE for — log + drop.
        logger.warning(
            "ALTER TABLE references unknown table %s; dropping action(s).",
            target_qn,
        )
        return

    table = tables[table_idx]
    new_fks: list[ForeignKey] = []
    for action in node.args.get("actions") or []:
        new_fks.extend(_fks_from_alter_action(action))

    if new_fks:
        tables[table_idx] = table.model_copy(
            update={"foreign_keys": [*table.foreign_keys, *new_fks]}
        )


def _fks_from_alter_action(action: exp.Expression) -> list[ForeignKey]:
    """Extract FKs from a single ALTER action node.

    sqlglot wraps ``ADD CONSTRAINT`` actions in :class:`exp.AddConstraint`
    whose ``expressions`` carry the actual :class:`exp.Constraint` nodes.
    """
    if not isinstance(action, exp.AddConstraint):
        return []
    found: list[ForeignKey] = []
    for sub in action.expressions:
        if isinstance(sub, exp.Constraint):
            cname = _identifier_name(sub.this) if sub.this is not None else ""
            for inner in sub.expressions:
                if isinstance(inner, exp.ForeignKey):
                    fk = _foreign_key_from_ast(inner, cname)
                    if fk is not None:
                        found.append(fk)
        elif isinstance(sub, exp.ForeignKey):
            # Unnamed ``ADD FOREIGN KEY ...`` — uncommon but supported.
            fk = _foreign_key_from_ast(sub, "")
            if fk is not None:
                found.append(fk)
    return found


# ---------------------------------------------------------------------------
# CREATE INDEX (deferred attach)
# ---------------------------------------------------------------------------


def _apply_index(
    node: exp.Create,
    tables: list[Table],
    table_index: dict[str, int],
) -> None:
    """Attach a ``CREATE INDEX`` to its owning table.

    Indexes targeting a table we never saw are logged + dropped, matching
    the ``_apply_alter`` policy.
    """
    idx_inner = node.this
    if not isinstance(idx_inner, exp.Index):
        return
    table_node = idx_inner.args.get("table")
    if not isinstance(table_node, exp.Table):
        return
    target_qn = _qn(_table_schema(table_node), table_node.name)
    table_idx = table_index.get(target_qn)
    if table_idx is None:
        logger.warning(
            "CREATE INDEX references unknown table %s; dropping.",
            target_qn,
        )
        return

    name = _identifier_name(idx_inner.this) if idx_inner.this is not None else ""
    cols = _index_columns(idx_inner.args.get("params"))
    if not cols:
        # An index with no resolvable columns is degenerate; skip.
        logger.warning("CREATE INDEX %s has no resolvable columns; dropping.", name)
        return
    unique = bool(node.args.get("unique"))
    kind = _index_kind_from_create(node)

    table = tables[table_idx]
    new_index = Index(name=name, columns=cols, unique=unique, kind=kind)
    tables[table_idx] = table.model_copy(update={"indexes": [*table.indexes, new_index]})


def _index_columns(params: object) -> list[str]:
    """Extract the column-list from an :class:`exp.IndexParameters`."""
    if not isinstance(params, exp.IndexParameters):
        return []
    cols: list[str] = []
    for o in params.args.get("columns") or []:
        if isinstance(o, exp.Ordered):
            inner = o.this
            if isinstance(inner, exp.Column):
                cols.append(inner.name)
            elif hasattr(inner, "name") and inner.name:
                cols.append(str(inner.name))
        elif isinstance(o, exp.Column):
            cols.append(o.name)
    return cols


def _index_kind_from_create(node: exp.Create) -> IndexKind:
    """Map the ``CREATE [...] INDEX`` ``kind`` modifier onto :data:`IndexKind`.

    T1 covers ``CLUSTERED`` / ``NONCLUSTERED`` (T-SQL) and plain
    ``INDEX``. Bitmap / hash / fulltext / spatial dialects either fall
    back to ``Command`` (skipped at the caller) or carry the modifier in
    ``IndexParameters.using`` — that branch is left ``"unknown"`` until a
    follow-up issue extends the matrix.
    """
    kind_str = (node.kind or "").upper()
    if kind_str == "CLUSTERED INDEX":
        return "clustered"
    if kind_str == "NONCLUSTERED INDEX":
        return "nonclustered"
    if kind_str in {"INDEX", "UNIQUE INDEX"}:
        return "btree"
    return "unknown"


# ---------------------------------------------------------------------------
# Small AST helpers
# ---------------------------------------------------------------------------


def _create_table_ref(node: exp.Create) -> tuple[str, str] | None:
    """Pull ``(schema, name)`` from a ``CREATE`` node's target.

    ``node.this`` is either an :class:`exp.Schema` (table / view body with
    columns) or an :class:`exp.Table` (sequence). Both expose a
    :class:`exp.Table` once you walk to ``.this``.
    """
    body = node.this
    table_node: exp.Table | None = None
    if isinstance(body, exp.Schema):
        inner = body.this
        if isinstance(inner, exp.Table):
            table_node = inner
    elif isinstance(body, exp.Table):
        table_node = body
    if table_node is None:
        return None
    schema_name = _table_schema(table_node)
    return schema_name, table_node.name


def _table_schema(table_node: exp.Table) -> str:
    """Return the schema-qualifier prefix for a :class:`exp.Table`.

    sqlglot stores the schema either as a bare string (DB2 ANSI parser
    path) or wrapped in an :class:`exp.Identifier` (T-SQL ``dbo.X``
    path). We project both onto a string so the Pydantic ``View`` /
    ``Table`` / ``Sequence`` schema field always sees ``str``.
    """
    db = table_node.args.get("db")
    if db is None:
        return ""
    if isinstance(db, str):
        return db
    name = getattr(db, "name", None)
    if isinstance(name, str):
        return name
    return ""


def _identifier_name(node: exp.Expression | None) -> str:
    """Return the textual name of an :class:`exp.Identifier` / Column / Table."""
    if node is None:
        return ""
    name = getattr(node, "name", None)
    if isinstance(name, str) and name:
        return name
    if hasattr(node, "sql"):
        return node.sql()
    return str(node)


def _columns_of(nodes: list[exp.Expression]) -> list[str]:
    """Return the textual names of a list of column-shaped AST nodes."""
    out: list[str] = []
    for n in nodes:
        if isinstance(n, exp.Column):
            out.append(n.name)
        elif isinstance(n, exp.Ordered):
            inner = n.this
            if isinstance(inner, exp.Column):
                out.append(inner.name)
        else:
            text = _identifier_name(n)
            if text:
                out.append(text)
    return out


__all__ = ["build_inventory"]

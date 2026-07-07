"""M2 Stage A — ``parse_stored_procs`` (sqlglot per CREATE PROCEDURE/FUNCTION/TRIGGER).

Walks ``--ddl-path`` (file or directory) and emits one :class:`ParsedSP`
per ``CREATE PROCEDURE`` / ``CREATE FUNCTION`` / ``CREATE TRIGGER``
statement found.

Critical design (issue #205): real legacy stored procedures use vendor
pragmas / DBMS packages that sqlglot may not recognise. Falling back
gracefully to ``parse_status="unparseable"`` (with ``raw_body``
preserved) lets the rest of the pipeline keep working on a partial
input; aborting the whole run on a single bad SP is the antipattern
this fallback exists to prevent.

The :class:`ParsedSP` dataclass is the *internal* shape Stages B/C/D
consume. The orchestrator (M2-10) folds these into the final
:class:`~primitives.stored_proc_analyze.StoredProc` Pydantic model.

Design spec §M2 Stage A (`docs/superpowers/specs/2026-05-22-data-modernization-design.md`).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError

from primitives._shared.sql_files import walk_sql_files as _walk_sql_files

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dialect mapping
# ---------------------------------------------------------------------------

# Public dialect strings (SPEC.md "Input flags") -> sqlglot dialect name.
# ``None`` selects sqlglot's ANSI parser, which is what we use for DB2
# because sqlglot 30.x does not ship a dedicated DB2 dialect. This matches
# the mapping used by ``tests/test_db_dumps_parse.py`` so the fixture
# smoke test and the parser stay in lockstep.
_DIALECT_TO_SQLGLOT: dict[str, str | None] = {
    "oracle": "oracle",
    "tsql": "tsql",
    "mssql": "tsql",  # alias — sqlglot uses "tsql"
    "db2": None,
}

# Dialects the auto-detector tries, in order of preference. Oracle and
# T-SQL come first because the bundled fixtures exercise their specific
# grammars; ANSI/None is the universal fallback.
_AUTO_DIALECTS: tuple[str, ...] = ("oracle", "tsql", "db2")

# Regex header used to slice a multi-SP file into one chunk per
# CREATE PROCEDURE/FUNCTION/TRIGGER. Anchored to start-of-line so a
# CREATE token embedded inside a comment or string literal does not
# trigger a false split. Handles the common variants:
#   CREATE PROCEDURE ...
#   CREATE OR REPLACE PROCEDURE ...
#   CREATE OR ALTER PROCEDURE ...   (T-SQL)
#   CREATE FUNCTION ... / CREATE TRIGGER ... (same modifiers)
_SP_HEADER_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?(PROCEDURE|FUNCTION|TRIGGER)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Sub-pattern used to extract the SP name from the header when sqlglot
# cannot parse the body. Captures an optional schema prefix
# (``dbo.``, ``HR.``) so the unparseable record still carries a useful
# identifier downstream. ``$`` is the only special character we accept
# inside identifiers (DB2 / Oracle allow it).
_SP_NAME_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+(?:REPLACE|ALTER)\s+)?"
    r"(?P<kind>PROCEDURE|FUNCTION|TRIGGER)\s+"
    r"(?:(?P<schema>[A-Za-z_$][A-Za-z0-9_$]*)\s*\.\s*)?"
    r"(?P<name>[A-Za-z_$][A-Za-z0-9_$]*)",
    re.IGNORECASE,
)

# Recognises a ``CREATE [OR REPLACE] FUNCTION`` header. sqlglot 30.x has
# no grammar for the Oracle ``CREATE FUNCTION … RETURN <type> IS|AS
# BEGIN … END`` body, so the whole statement degrades to an
# :class:`exp.Command` / :class:`exp.Block` node (or raises a
# ``ParseError``) even though the *inner* SQL parses fine. This anchor
# lets ``_parse_chunk`` tell a recognisable-but-unsupported FUNCTION
# header apart from genuinely garbage input, so it can attempt body
# recovery instead of giving up (issue #852). Anchored to start-of-chunk;
# ``OR REPLACE`` is the only modifier Oracle functions use.
_CREATE_FUNCTION_RE = re.compile(
    r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b",
    re.IGNORECASE,
)

# SQL statement leaders we try to lift out of an unsupported function
# body during recovery (issue #852). Each embedded query is parsed in
# isolation — from the keyword to the next ``;`` — which sidesteps the
# PL/SQL procedural wrapper (``IS|AS … BEGIN … RETURN … END``) sqlglot
# cannot model. ``WITH`` covers CTE-prefixed selects.
_RECOVERABLE_STMT_RE = re.compile(
    r"\b(?:SELECT|WITH|INSERT|UPDATE|DELETE|MERGE)\b",
    re.IGNORECASE,
)

SPKind = Literal["PROCEDURE", "FUNCTION", "TRIGGER"]
ParseStatus = Literal["parsed", "unparseable"]


# ---------------------------------------------------------------------------
# ParsedSP — internal stage-A output
# ---------------------------------------------------------------------------


@dataclass
class ParsedSP:
    """Stage A output for a single stored procedure / function / trigger.

    This is the **internal** shape Stages B (static profile), C (IO
    classification) and D (side-effect surface) consume. The orchestrator
    folds these into the final
    :class:`~primitives.stored_proc_analyze.StoredProc` Pydantic record.

    ``ast`` is ``None`` when ``parse_status == "unparseable"`` — that case
    is what the fallback exists to support. Downstream stages must check
    ``parse_status`` before walking the AST.

    ``source_dialect`` is the sqlglot dialect string (``"oracle"`` /
    ``"tsql"`` / ``"db2"``) that produced ``ast``. ``"db2"`` always
    appears as ``None`` to sqlglot internally but is preserved as the
    public string here so the digest's ``source_dialect`` field is
    truthful (it's not a literal ``None``).
    """

    name: str
    schema: str | None
    kind: SPKind
    raw_body: str
    source_dialect: str
    parse_status: ParseStatus
    ast: exp.Expression | None = None
    source_path: Path | None = None
    parse_error: str | None = None
    # Free-form notes the stage emits when a graceful degradation occurs
    # (e.g. ``sqlglot fell back to Command``). Kept as a list so multiple
    # stages can append without trampling each other.
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ParsedSP:
        """Reconstruct a :class:`ParsedSP` from a serialised dict.

        Mirrors Pydantic's ``model_validate`` for the ``--continue`` path:
        the dict must carry the same keys that ``--extract-only`` persists
        via ``save_intermediate("parsed_sps", …)``.  ``ast`` is always
        restored as ``None`` because AST objects are not JSON-serialisable;
        downstream stages in the ``--continue`` flow do not require them.
        """
        return cls(
            name=str(raw["name"]),
            schema=cast("str | None", raw.get("schema")),
            kind=cast("SPKind", raw["kind"]),
            source_dialect=str(raw["source_dialect"]),
            raw_body=str(raw["raw_body"]),
            parse_status=cast("ParseStatus", raw["parse_status"]),
            ast=None,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_stored_procs(ddl_path: Path, dialect: str = "auto") -> list[ParsedSP]:
    """Walk ``ddl_path`` and emit one :class:`ParsedSP` per SP found.

    Args:
        ddl_path: A file (``.sql``) or a directory walked recursively for
            ``.sql`` files. Symlinks are not followed (matches M1
            behaviour — see ``db_schema_analyze`` SPEC).
        dialect: ``"auto"`` (default), ``"oracle"``, ``"tsql"`` /
            ``"mssql"``, or ``"db2"``. ``"auto"`` runs the input through
            each dialect in :data:`_AUTO_DIALECTS` order and picks the
            one with the most successful parses per file.

    Returns:
        One :class:`ParsedSP` per ``CREATE PROCEDURE`` / ``CREATE
        FUNCTION`` / ``CREATE TRIGGER`` statement discovered. Empty
        list when no SPs are present. Order matches file walk + textual
        order within each file.

    Raises:
        FileNotFoundError: If ``ddl_path`` does not exist.
        ValueError: If ``dialect`` is not one of the supported strings.
    """
    if not ddl_path.exists():
        raise FileNotFoundError(f"--ddl-path not found: {ddl_path}")

    if dialect != "auto" and dialect not in _DIALECT_TO_SQLGLOT:
        raise ValueError(
            f"unsupported dialect {dialect!r}; "
            f"expected one of: auto, {', '.join(sorted(_DIALECT_TO_SQLGLOT))}"
        )

    results: list[ParsedSP] = []
    for sql_file in _walk_sql_files(ddl_path):
        text = sql_file.read_text(encoding="utf-8")
        effective_dialect = _detect_dialect(text) if dialect == "auto" else dialect
        for chunk in _split_into_sp_chunks(text):
            results.append(_parse_chunk(chunk, effective_dialect, sql_file))
    return results


# ---------------------------------------------------------------------------
# Splitter
# ---------------------------------------------------------------------------


def _split_into_sp_chunks(text: str) -> list[str]:
    """Slice ``text`` into one chunk per CREATE PROCEDURE/FUNCTION/TRIGGER.

    Splitting at the regex boundary (rather than trying to parse the
    whole file in one sqlglot call) is the key to graceful per-SP
    degradation: one broken proc can be tagged ``unparseable`` without
    poisoning every subsequent proc in the file.
    """
    starts = [m.start() for m in _SP_HEADER_RE.finditer(text)]
    if not starts:
        return []
    starts.append(len(text))
    chunks: list[str] = []
    for i in range(len(starts) - 1):
        chunk = text[starts[i] : starts[i + 1]].strip()
        if chunk:
            chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Per-chunk parsing
# ---------------------------------------------------------------------------


def _parse_chunk(chunk: str, dialect: str, source_path: Path) -> ParsedSP:
    """Parse a single CREATE statement chunk with graceful fallback."""
    name, schema, kind = _extract_header_metadata(chunk)
    sqlglot_dialect = _DIALECT_TO_SQLGLOT.get(dialect)

    try:
        ast = sqlglot.parse_one(chunk, dialect=sqlglot_dialect)
    except (ParseError, SqlglotError) as exc:
        # sqlglot's ``CREATE FUNCTION … RETURN <type>`` grammar gap can
        # surface as a hard ``ParseError`` (e.g. an embedded ``SELECT …
        # INTO``) rather than a silent ``Command`` fallback. Attempt the
        # same body-recovery salvage before declaring the function
        # unparseable (issue #852); genuinely garbage input still lands
        # as ``unparseable`` because the recovery returns ``None``.
        recovered = _recover_function_body(chunk, kind, sqlglot_dialect)
        if recovered is not None:
            return _recovered_parsed_sp(
                name, schema, kind, chunk, dialect, source_path, recovered, str(exc)
            )
        logger.warning(
            "sqlglot ParseError on %s in %s: %s",
            name,
            source_path,
            exc,
        )
        return ParsedSP(
            name=name,
            schema=schema,
            kind=kind,
            raw_body=chunk,
            source_dialect=dialect,
            parse_status="unparseable",
            ast=None,
            source_path=source_path,
            parse_error=str(exc),
        )

    if ast is None:
        logger.warning("sqlglot returned None for %s in %s", name, source_path)
        return ParsedSP(
            name=name,
            schema=schema,
            kind=kind,
            raw_body=chunk,
            source_dialect=dialect,
            parse_status="unparseable",
            ast=None,
            source_path=source_path,
            parse_error="sqlglot returned None",
        )

    # sqlglot may *silently* fall back to a Command/Block node when the
    # body uses unsupported syntax (it logs a "contains unsupported
    # syntax" warning but does not raise). From a downstream perspective
    # that fallback is indistinguishable from a parse failure — Stages
    # B/C/D need a real :class:`exp.Create` node to walk for tables,
    # cursors, EXEC out-calls, etc. We therefore tag the fallback as
    # ``unparseable`` and preserve the raw body, matching the issue
    # #205 contract ("falling back gracefully beats crashing").
    if not isinstance(ast, exp.Create):
        # ``CREATE [OR REPLACE] FUNCTION … RETURN <type>`` bodies have no
        # sqlglot grammar, so the whole statement degrades to a Command /
        # Block node even though the embedded SQL parses fine. Rather than
        # zeroing every downstream metric, attempt a salvage pass that
        # re-parses the inner SELECT / DML statements into a synthetic
        # block the profiler can walk (issue #852). Genuinely garbage
        # input falls through to ``unparseable`` because the recovery
        # returns ``None``.
        recovered = _recover_function_body(chunk, kind, sqlglot_dialect)
        if recovered is not None:
            return _recovered_parsed_sp(
                name,
                schema,
                kind,
                chunk,
                dialect,
                source_path,
                recovered,
                f"sqlglot fell back to {type(ast).__name__} (grammar gap)",
            )
        logger.warning(
            "sqlglot fell back to %s for %s in %s — treating as unparseable",
            type(ast).__name__,
            name,
            source_path,
        )
        return ParsedSP(
            name=name,
            schema=schema,
            kind=kind,
            raw_body=chunk,
            source_dialect=dialect,
            parse_status="unparseable",
            ast=None,
            source_path=source_path,
            parse_error=f"sqlglot fell back to {type(ast).__name__} (grammar gap)",
        )

    # Prefer the parsed AST's name when sqlglot recognised the statement
    # — it normalises quoting and case the way downstream tooling
    # expects. Header-derived metadata is the fallback.
    parsed_name, parsed_schema = _extract_ast_metadata(ast)
    if parsed_name:
        name = parsed_name
    if parsed_schema is not None:
        schema = parsed_schema

    return ParsedSP(
        name=name,
        schema=schema,
        kind=kind,
        raw_body=chunk,
        source_dialect=dialect,
        parse_status="parsed",
        ast=ast,
        source_path=source_path,
    )


# ---------------------------------------------------------------------------
# CREATE FUNCTION body recovery (issue #852)
# ---------------------------------------------------------------------------


def _recover_function_body(
    chunk: str, kind: SPKind, sqlglot_dialect: str | None
) -> exp.Expression | None:
    """Salvage the inner SQL of a sqlglot-unsupported ``CREATE FUNCTION``.

    sqlglot 30.x has no grammar for the Oracle ``CREATE FUNCTION …
    RETURN <type> IS|AS BEGIN … END`` body, so the statement degrades to
    a Command / Block node (or raises). The procedural wrapper is what
    sqlglot cannot model — the *embedded* SQL (the ``SELECT`` driving the
    function's value, any DML, the CASE ladder) parses cleanly on its
    own. This helper re-parses each embedded statement in isolation and
    wraps the survivors in a synthetic :class:`exp.Block` the downstream
    profiler (Stage B) and IO classifier (Stage C) can walk for ``loc``,
    ``cyclomatic``, touched tables, IO shape and side effects.

    Returns the synthetic block when at least one inner statement was
    recovered, else ``None`` — the caller treats ``None`` as "fall back
    to the conservative ``unparseable`` status" so genuinely garbage
    ``CREATE FUNCTION``-shaped input is *not* silently upgraded.

    Recovery is scoped to ``FUNCTION`` chunks (the issue #852 grammar
    gap); a procedure that degrades to Command keeps its existing
    ``unparseable`` behaviour because procedures already parse to
    ``exp.Create`` when supported.
    """
    if kind != "FUNCTION" or not _CREATE_FUNCTION_RE.match(chunk):
        return None

    recovered: list[exp.Expression] = []
    seen_spans: set[tuple[int, int]] = set()
    for match in _RECOVERABLE_STMT_RE.finditer(chunk):
        start = match.start()
        # Slice from the statement leader to the next ``;`` terminator (or
        # end-of-chunk). The PL/SQL wrapper (``BEGIN``/``RETURN``/``END``)
        # sits outside this slice, so what remains is a free-standing
        # query sqlglot can parse.
        end = chunk.find(";", start)
        segment = chunk[start:] if end == -1 else chunk[start:end]
        segment = _balance_trailing_parens(segment).strip()
        if not segment:
            continue
        span = (start, start + len(segment))
        if span in seen_spans:
            continue
        seen_spans.add(span)
        try:
            node = sqlglot.parse_one(segment, dialect=sqlglot_dialect)
        except (ParseError, SqlglotError):
            continue
        # A salvaged fragment that *itself* degrades to a Command carries
        # no walkable structure, so it is not a useful recovery.
        if node is None or isinstance(node, exp.Command):
            continue
        recovered.append(cast("exp.Expression", node))

    if not recovered:
        return None
    return exp.Block(expressions=recovered)


def _balance_trailing_parens(segment: str) -> str:
    """Drop unmatched trailing ``)`` left by slicing inside a wrapper.

    A function body such as ``RETURN (SELECT … FROM t)`` yields the slice
    ``SELECT … FROM t)`` once the leading ``RETURN (`` is stripped — the
    dangling ``)`` would make sqlglot reject the fragment. Trim trailing
    close-parens only while the fragment is paren-unbalanced, so a
    legitimately balanced ``func(args)`` tail is preserved.
    """
    while segment.rstrip().endswith(")") and segment.count("(") < segment.count(")"):
        segment = segment.rstrip()[:-1]
    return segment


def _recovered_parsed_sp(
    name: str,
    schema: str | None,
    kind: SPKind,
    chunk: str,
    dialect: str,
    source_path: Path,
    recovered_ast: exp.Expression,
    provenance: str,
) -> ParsedSP:
    """Build a ``parse_status="parsed"`` record from a recovered body.

    The status stays ``"parsed"`` (the existing :data:`ParseStatus`
    Literal has no softer value) because the downstream metrics *do*
    compute off ``recovered_ast``; a ``notes`` entry records that the
    ``CREATE FUNCTION`` header was not fully parsed so reviewers can see
    the recovery happened. ``parse_error`` carries the original sqlglot
    diagnostic for the same audit reason.
    """
    note = (
        "recovered CREATE FUNCTION body via inner-statement salvage "
        f"(header unparsed by sqlglot: {provenance})"
    )
    logger.info("recovered function body for %s in %s (%s)", name, source_path, provenance)
    return ParsedSP(
        name=name,
        schema=schema,
        kind=kind,
        raw_body=chunk,
        source_dialect=dialect,
        parse_status="parsed",
        ast=recovered_ast,
        source_path=source_path,
        parse_error=provenance,
        notes=[note],
    )


# ---------------------------------------------------------------------------
# Header / AST metadata extraction
# ---------------------------------------------------------------------------


def _extract_header_metadata(chunk: str) -> tuple[str, str | None, SPKind]:
    """Pull (name, schema, kind) from the chunk's CREATE header.

    Used both as the primary identifier when sqlglot fails to parse and
    as a sanity check against the AST's view of the same fields.
    """
    match = _SP_NAME_RE.match(chunk)
    if match is None:
        # Should not happen because the chunk was selected by the same
        # header regex family — but guard anyway so the dataclass always
        # has a non-empty name.
        return "<unknown>", None, "PROCEDURE"
    kind_str = match.group("kind").upper()
    # cast back to the Literal narrow type
    if kind_str not in ("PROCEDURE", "FUNCTION", "TRIGGER"):  # pragma: no cover
        kind_str = "PROCEDURE"
    kind: SPKind = kind_str  # type: ignore[assignment]
    return match.group("name"), match.group("schema"), kind


def _extract_ast_metadata(ast: exp.Expression) -> tuple[str | None, str | None]:
    """Pull (name, schema) from the parsed AST when possible."""
    if not isinstance(ast, exp.Create):
        return None, None
    head = ast.this
    # Oracle / DB2 / ANSI: UserDefinedFunction(this=Table(...))
    if isinstance(head, exp.UserDefinedFunction):
        table = head.this
        if isinstance(table, exp.Table):
            return table.name, _identifier_name(table.args.get("db"))
        if isinstance(table, exp.Identifier):
            return table.this, None
    # T-SQL: StoredProcedure(this=Table(...))
    if isinstance(head, exp.StoredProcedure):
        table = head.this
        if isinstance(table, exp.Table):
            return table.name, _identifier_name(table.args.get("db"))
    # Triggers / generic Create: head may be a Table directly
    if isinstance(head, exp.Table):
        return head.name, _identifier_name(head.args.get("db"))
    return None, None


def _identifier_name(node: exp.Expression | None) -> str | None:
    """Return the textual name from an :class:`exp.Identifier` (or ``None``)."""
    if isinstance(node, exp.Identifier):
        # ``node.this`` is typed Any by sqlglot but is always a str for Identifier.
        value = node.this
        return value if isinstance(value, str) else None
    return None


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def _detect_dialect(text: str) -> str:
    """Run ``text`` through each candidate dialect and pick the best.

    "Best" = highest count of chunks that parse as a top-level
    :class:`exp.Create` (i.e. sqlglot's actual grammar matched rather
    than falling back to ``Command`` / ``Block``). Ties prefer the
    order in :data:`_AUTO_DIALECTS` (Oracle > T-SQL > DB2/ANSI).
    """
    chunks = _split_into_sp_chunks(text)
    if not chunks:
        return _AUTO_DIALECTS[-1]  # default to ANSI

    best_dialect = _AUTO_DIALECTS[0]
    best_score = -1
    for candidate in _AUTO_DIALECTS:
        sqlglot_dialect = _DIALECT_TO_SQLGLOT.get(candidate)
        score = 0
        for chunk in chunks:
            try:
                ast = sqlglot.parse_one(chunk, dialect=sqlglot_dialect)
            except (ParseError, SqlglotError):
                continue
            if isinstance(ast, exp.Create):
                score += 1
        if score > best_score:
            best_score = score
            best_dialect = candidate
    return best_dialect


__all__ = ["ParseStatus", "ParsedSP", "SPKind", "parse_stored_procs"]

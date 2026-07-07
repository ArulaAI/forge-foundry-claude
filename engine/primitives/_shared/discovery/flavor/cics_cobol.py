"""M5 Phase B flavor pack: CICS / COBOL regex extractor (M5-FLAVOR-1, #446).

This module is the v1 ``cics-cobol`` parser the M5 Phase B
(``shared/analyze.py``) drives over each COBOL source file in the
``--code-root`` tree. It produces *structured records* (not raw match
spans) so the Phase B aggregator can plug the output straight into the
canonical Pydantic surface from ``shared/models.py``:

* ``EXEC CICS LINK`` / ``EXEC CICS XCTL`` → ``ProgramRecord.calls_to``
  edges (paired with the calling program's ``called_by`` back-pointer in
  the Phase B aggregator).
* ``CALL "PROG"`` / ``CALL VAR`` → COBOL static / dynamic call edges
  (same downstream sink as LINK / XCTL — they're both outgoing calls,
  just with weaker CICS semantics).
* ``COPY copybook`` → ``ProgramRecord.copybook_deps`` (forward edge);
  the backward ``CopybookRecord.used_by_programs`` is filled by the
  aggregator.
* ``EXEC CICS READ`` / ``WRITE`` / ``REWRITE`` / ``DELETE`` /
  ``READNEXT`` / ``READPREV`` / ``STARTBR`` / ``ENDBR`` →
  ``VSAMFileRecord.accessed_by[program] += [mode]``.
* ``EXEC SQL`` → embedded SQL statement records (table names extracted
  from ``FROM`` / ``UPDATE`` / ``INSERT INTO`` / ``DELETE FROM``).
* ``EXEC CICS SEND MAP`` / ``RECEIVE MAP`` → ``BMSMapRecord.maps`` +
  per-transaction mapping (the per-program back-reference for §7
  Screen Navigation rendering).
* ``EXEC CICS SIGNON`` / ``EXEC CICS VERIFY PASSWORD`` → security
  operation records that flow into ``AnalysisDigest.security_findings``.
* ``EXEC CICS ABEND`` / ``EXEC CICS HANDLE ABEND`` → error-path records
  feeding §10 Security Architecture + §13 Operational Patterns.

The extractor is a *line-level regex pack* (not a full COBOL parser).
This is the deliberate v1 scope per the design spec
(``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
§"M5-FLAVOR-1"): real-world parser coverage demands an AST grammar that
would balloon scope. The regex pack ships honest precision (every
positive case sampled from BankDemo) at the cost of imperfect recall on
heavily macro-ed sources — which Phase D's self-audit (M5-AUDIT-1)
catches and surfaces in §Validation Report rather than silently dropping.

Key COBOL conventions the pack respects:

* **Comment lines.** Column 7 is the *indicator area*; an ``*`` (or
  ``/``) in column 7 marks a comment line. The pack skips lines whose
  *seventh character* is one of these markers BEFORE the rest of the
  matching pipeline sees them. Crucially, leading whitespace counted
  via ``len(line) - len(line.lstrip(' '))`` is not enough: COBOL columns
  are *fixed*, so we index on the 7-character indicator slice.
* **Continuation.** ``EXEC CICS`` / ``EXEC SQL`` statements span
  multiple lines and terminate on ``END-EXEC`` (with or without a
  trailing period). The pack joins these into a single *logical line*
  before regex-matching, while preserving the **starting line number**
  so the evidence-ledger row (M5-LEDGER-1) cites the verb's first line.
* **Mixed case.** Modern COBOL dialects accept lower-case keywords;
  the regex bank is case-insensitive on verbs (LINK / XCTL / READ / …)
  but case-preserving on identifiers (program names, file names, table
  names) because those round-trip into the playbook narrative verbatim.
* **Variable program names.** ``EXEC CICS LINK PROGRAM(WS-NEXT-PROG)``
  is recorded as a *dynamic* edge — the resolved target name is
  unknown until the Phase B aggregator cross-references the COBOL
  WORKING-STORAGE assignment for ``WS-NEXT-PROG``. This module just
  carries the variable name verbatim and tags the record's
  ``resolved=False``; the aggregator decides whether to leave it
  unresolved (and let Phase D flag the orphan) or to resolve it via the
  evidence ledger.

The per-file public entry is :func:`parse_cobol_source`. The lower-level
:func:`extract_*` functions are exposed for unit testing + targeted
re-use (a Phase D auditor that wants to spot-check one file without
running the whole pipeline).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.discovery.models import Confidence

# ---------------------------------------------------------------------------
# Constants — COBOL column conventions
# ---------------------------------------------------------------------------

#: Column 7 (0-indexed = 6) is the COBOL indicator area. ``*`` or ``/``
#: in this column marks a comment line; ``-`` marks a continuation line.
_COMMENT_INDICATORS: Final[frozenset[str]] = frozenset({"*", "/"})

#: 1-based COBOL column where the indicator area sits. Lines must be at
#: least this many characters to even have an indicator column to test.
_INDICATOR_COLUMN: Final[int] = 7

#: 0-based offset of the COBOL Area A start (column 8 in the spec).
#: Source text from this offset onwards is program text proper.
_AREA_A_START: Final[int] = 7

#: VSAM access modes the M5 design spec calls out. The extractor records
#: each verb verbatim so downstream §6 (Data Architecture) renders the
#: per-program access matrix without re-interpreting CICS dialect.
_VSAM_VERBS: Final[tuple[str, ...]] = (
    "READ",
    "READNEXT",
    "READPREV",
    "WRITE",
    "REWRITE",
    "DELETE",
    "STARTBR",
    "ENDBR",
    "UNLOCK",
)

VsamAccessMode = Literal[
    "READ", "READNEXT", "READPREV", "WRITE", "REWRITE", "DELETE", "STARTBR", "ENDBR", "UNLOCK"
]

# ---------------------------------------------------------------------------
# Record dataclasses — structured output shapes
# ---------------------------------------------------------------------------
#
# These are deliberately *separate* from the Pydantic models in
# shared/models.py because:
#
# 1. They're per-file extraction records; the Pydantic models aggregate
#    across the whole code root.
# 2. They carry ``line`` numbers (1-based) the Phase B aggregator uses
#    to build EvidenceRow citations. The Pydantic models don't carry
#    per-program line numbers because a ProgramRecord is already keyed
#    on ``source_path`` + ``name``.
# 3. Keeping them as frozen dataclasses (vs Pydantic) saves
#    instantiation cost — Phase B walks thousands of files and the
#    regex output dominates the hot path.


@dataclass(frozen=True)
class ProgramCallRecord:
    """One outgoing program call edge extracted from one source file.

    ``call_kind`` records *which* CICS verb the source used; the
    aggregator collapses ``link`` / ``xctl`` / ``call_static`` /
    ``call_dynamic`` into ``ProgramRecord.calls_to`` but the playbook
    §5 Call Chain Map renders the kind as an edge label.
    """

    call_kind: Literal["link", "xctl", "call_static", "call_dynamic"]
    target: str
    """Program name (literal) or variable name (dynamic)."""
    resolved: bool
    """``True`` for ``'NAME'`` literals; ``False`` for variable refs."""
    line: int


@dataclass(frozen=True)
class CopybookIncludeRecord:
    """One ``COPY copybook`` inclusion extracted from one source file."""

    name: str
    line: int


@dataclass(frozen=True)
class VsamAccessRecord:
    """One ``EXEC CICS READ/WRITE/REWRITE/DELETE`` file access record."""

    file_name: str
    mode: VsamAccessMode
    resolved: bool
    """``True`` for ``'NAME'`` literals; ``False`` for variable refs."""
    line: int


@dataclass(frozen=True)
class SqlStatementRecord:
    """One embedded ``EXEC SQL`` statement extracted from one source file.

    ``statement_kind`` is the SQL verb (``SELECT`` / ``UPDATE`` /
    ``INSERT`` / ``DELETE`` / ``DECLARE`` / ``OPEN`` / ``FETCH`` /
    ``CLOSE`` / ``INCLUDE`` / ``BEGIN`` / ``OTHER``). ``tables`` is the
    sorted list of unique table identifiers discovered via the
    ``FROM`` / ``UPDATE`` / ``INSERT INTO`` / ``DELETE FROM`` clauses.
    ``raw`` is the joined logical-line body (used by Phase C renderers
    that want to quote the SQL verbatim).
    """

    statement_kind: Literal[
        "SELECT",
        "UPDATE",
        "INSERT",
        "DELETE",
        "DECLARE",
        "OPEN",
        "FETCH",
        "CLOSE",
        "INCLUDE",
        "BEGIN",
        "OTHER",
    ]
    tables: tuple[str, ...]
    raw: str
    line: int


@dataclass(frozen=True)
class BmsInteractionRecord:
    """One ``EXEC CICS SEND MAP`` / ``RECEIVE MAP`` interaction."""

    direction: Literal["SEND", "RECEIVE"]
    map_name: str
    mapset: str | None
    """``None`` when the source omits ``MAPSET(...)`` (CICS allows it)."""
    resolved: bool
    line: int


@dataclass(frozen=True)
class SecurityOperationRecord:
    """One ``EXEC CICS SIGNON`` / ``EXEC CICS VERIFY PASSWORD`` operation.

    ``userid`` / ``password_ref`` carry the *operand* the source passed
    (usually a working-storage field name like ``CPSWDD01I-USERID``).
    The aggregator can decide whether to resolve these against the
    copybook layout — this module just surfaces the literal operand.
    """

    operation: Literal["SIGNON", "VERIFY_PASSWORD"]
    userid: str | None
    password_ref: str | None
    line: int


@dataclass(frozen=True)
class AbendPathRecord:
    """One ``EXEC CICS ABEND`` / ``EXEC CICS HANDLE ABEND`` record.

    ``kind`` partitions explicit aborts from declared handlers. The
    Phase C §10 / §13 renderers render handlers as recoverable paths
    and explicit aborts as terminal error paths.
    """

    kind: Literal["ABEND", "HANDLE_ABEND"]
    code: str | None
    """``ABCODE(...)`` operand for ``ABEND``; handler label for
    ``HANDLE ABEND`` (``LABEL(...)`` / ``PROGRAM(...)``)."""
    line: int


# ---------------------------------------------------------------------------
# Regex pack — verb-keyed compiled patterns
# ---------------------------------------------------------------------------
#
# Each regex is compiled once at module import and reused across every
# file the Phase B aggregator drives. The patterns target *logical
# lines* (after continuation joining + comment stripping) — see
# :func:`_iter_statements` for the pre-processing rules.


# --- EXEC CICS verbs --------------------------------------------------------

#: EXEC CICS LINK PROGRAM(name) — captures both quoted literals and
#: variable references. Group ``literal`` matches the quoted form,
#: ``var`` matches the bare identifier.
_EXEC_CICS_LINK_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+CICS\s+LINK\b"
    r".*?\bPROGRAM\s*\(\s*"
    r"(?:'(?P<lit_sq>[^']+)'|"
    r"\"(?P<lit_dq>[^\"]+)\"|"
    r"(?P<var>[A-Za-z][A-Za-z0-9\-]*))"
    r"\s*\)",
    re.IGNORECASE | re.DOTALL,
)

#: EXEC CICS XCTL PROGRAM(name) — same operand shapes as LINK.
_EXEC_CICS_XCTL_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+CICS\s+XCTL\b"
    r".*?\bPROGRAM\s*\(\s*"
    r"(?:'(?P<lit_sq>[^']+)'|"
    r"\"(?P<lit_dq>[^\"]+)\"|"
    r"(?P<var>[A-Za-z][A-Za-z0-9\-]*))"
    r"\s*\)",
    re.IGNORECASE | re.DOTALL,
)

#: EXEC CICS READ/READNEXT/WRITE/REWRITE/DELETE/STARTBR/ENDBR/UNLOCK
#: FILE(name) — the verb captured in group ``verb``, file name in
#: ``lit_*`` / ``var``. The verb alternation is built dynamically from
#: :data:`_VSAM_VERBS` so adding a new dialect verb is a one-line edit.
_EXEC_CICS_VSAM_VERBS_GROUP = "|".join(_VSAM_VERBS)
_EXEC_CICS_VSAM_RE: Final[re.Pattern[str]] = re.compile(
    rf"\bEXEC\s+CICS\s+(?P<verb>{_EXEC_CICS_VSAM_VERBS_GROUP})\b"
    r".*?\bFILE\s*\(\s*"
    r"(?:'(?P<lit_sq>[^']+)'|"
    r"\"(?P<lit_dq>[^\"]+)\"|"
    r"(?P<var>[A-Za-z][A-Za-z0-9\-]*))"
    r"\s*\)",
    re.IGNORECASE | re.DOTALL,
)

#: EXEC CICS SEND MAP / RECEIVE MAP — the direction captured in
#: ``direction``, map name in ``lit_*`` / ``var`` (the operand directly
#: after the ``MAP`` keyword), optional mapset in the ``mapset_*``
#: groups. The verb form is ``EXEC CICS {SEND|RECEIVE} MAP(<name>)``
#: where the parenthesised operand IS the ``MAP`` operand — there's no
#: separate "look for another ``MAP(...)`` further along" step.
_EXEC_CICS_BMS_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+CICS\s+(?P<direction>SEND|RECEIVE)\s+MAP\s*\(\s*"
    r"(?:'(?P<lit_sq>[^']+)'|"
    r"\"(?P<lit_dq>[^\"]+)\"|"
    r"(?P<var>[A-Za-z][A-Za-z0-9\-]*))"
    r"\s*\)"
    r"(?:.*?\bMAPSET\s*\(\s*"
    r"(?:'(?P<mapset_sq>[^']+)'|"
    r"\"(?P<mapset_dq>[^\"]+)\"|"
    r"(?P<mapset_var>[A-Za-z][A-Za-z0-9\-]*))"
    r"\s*\))?",
    re.IGNORECASE | re.DOTALL,
)

#: EXEC CICS SIGNON — pulls USERID + PASSWORD operands optionally.
_EXEC_CICS_SIGNON_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+CICS\s+SIGNON\b"
    r"(?:.*?\bUSERID\s*\(\s*"
    r"(?P<userid>[^)]+?)"
    r"\s*\))?"
    r"(?:.*?\bPASSWORD\s*\(\s*"
    r"(?P<password>[^)]+?)"
    r"\s*\))?",
    re.IGNORECASE | re.DOTALL,
)

#: EXEC CICS VERIFY PASSWORD — pulls USERID + PASSWORD operands.
_EXEC_CICS_VERIFY_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+CICS\s+VERIFY\s+PASSWORD\b"
    r"(?:.*?\bUSERID\s*\(\s*"
    r"(?P<userid>[^)]+?)"
    r"\s*\))?"
    r"(?:.*?\bPASSWORD\s*\(\s*"
    r"(?P<password>[^)]+?)"
    r"\s*\))?",
    re.IGNORECASE | re.DOTALL,
)

#: EXEC CICS ABEND — captures the ABCODE operand.
_EXEC_CICS_ABEND_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+CICS\s+ABEND\b"
    r"(?:.*?\bABCODE\s*\(\s*"
    r"(?:'(?P<lit_sq>[^']+)'|"
    r"\"(?P<lit_dq>[^\"]+)\"|"
    r"(?P<var>[A-Za-z][A-Za-z0-9\-]*))"
    r"\s*\))?",
    re.IGNORECASE | re.DOTALL,
)

#: EXEC CICS HANDLE ABEND — captures the LABEL / PROGRAM operand.
_EXEC_CICS_HANDLE_ABEND_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+CICS\s+HANDLE\s+ABEND\b"
    r"(?:.*?\b(?:LABEL|PROGRAM)\s*\(\s*"
    r"(?:'(?P<lit_sq>[^']+)'|"
    r"\"(?P<lit_dq>[^\"]+)\"|"
    r"(?P<var>[A-Za-z][A-Za-z0-9\-]*))"
    r"\s*\))?",
    re.IGNORECASE | re.DOTALL,
)


# --- EXEC SQL ---------------------------------------------------------------

#: EXEC SQL statement — the first non-blank token after EXEC SQL is the
#: SQL verb. We capture it in group ``verb``.
_EXEC_SQL_RE: Final[re.Pattern[str]] = re.compile(
    r"\bEXEC\s+SQL\b\s*(?P<body>.*)",
    re.IGNORECASE | re.DOTALL,
)

#: SQL FROM / UPDATE / INSERT INTO / DELETE FROM table extraction. The
#: identifier rule is intentionally permissive: SQL identifiers can
#: include underscores, digits (not as first char), and dotted
#: schema.table notation.
_SQL_TABLE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:FROM|UPDATE|INTO|DELETE\s+FROM)\s+"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_\.]*)",
    re.IGNORECASE,
)


# --- COBOL CALL + COPY ------------------------------------------------------

#: COBOL CALL statement — ``CALL 'PROG'`` (static, quoted) or
#: ``CALL VAR`` (dynamic). Group ``lit_*`` for static, ``var`` for
#: dynamic.
_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"\bCALL\s+"
    r"(?:'(?P<lit_sq>[^']+)'|"
    r"\"(?P<lit_dq>[^\"]+)\"|"
    r"(?P<var>[A-Za-z][A-Za-z0-9\-]*))",
    re.IGNORECASE,
)

#: COBOL COPY statement — ``COPY NAME.`` or ``COPY NAME OF LIB.``
#: terminating period optional inside a statement. We capture only the
#: bare copybook name; ``OF LIB`` qualifier is ignored at v1.
_COPY_RE: Final[re.Pattern[str]] = re.compile(
    r"\bCOPY\s+(?P<name>[A-Za-z][A-Za-z0-9\-]*)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Pre-processing — comment stripping + continuation joining
# ---------------------------------------------------------------------------


def _is_comment_line(line: str) -> bool:
    """Return True if column 7 of ``line`` is a COBOL comment indicator.

    COBOL fixed-format places the *indicator area* at column 7 (0-indexed
    position 6). ``*`` or ``/`` there marks the whole line as a comment.
    Lines shorter than 7 characters cannot be comment lines (they are
    blank lines or partial-column source).
    """
    if len(line) < _INDICATOR_COLUMN:
        return False
    return line[_INDICATOR_COLUMN - 1] in _COMMENT_INDICATORS


@dataclass(frozen=True)
class _LogicalStatement:
    """One pre-processed logical statement = joined source lines.

    ``body`` is the concatenated text (comments + indicator column
    stripped, continuation lines joined). ``start_line`` is the 1-based
    line number of the *first* non-comment source line that contributed
    to this statement — what evidence-ledger citations point at.
    """

    body: str
    start_line: int


def _iter_statements(content: str) -> list[_LogicalStatement]:
    """Yield logical statements over ``content``.

    Splits on ``END-EXEC`` for ``EXEC CICS`` / ``EXEC SQL`` blocks, and
    on ``.`` (period) for COBOL statements. Continuation across lines
    is handled by joining all in-block source lines with single spaces.

    Comment lines (column 7 = ``*`` or ``/``) are filtered before
    joining so a commented-out ``EXEC CICS LINK`` inside a continuation
    block is not picked up.

    The algorithm:

    1. Walk source line-by-line.
    2. Skip comment-indicator lines entirely.
    3. Buffer lines into a statement. When the buffer reaches
       ``END-EXEC`` (case-insensitive, possibly trailed by ``.``) or a
       sentence period, emit a statement.

    The implementation is intentionally simple: it does *not* attempt
    to parse the indicator column for ``-`` (continuation) tokens —
    COBOL continuation is mostly used for long literals, not for verb
    bodies. Phase B treats the joined-block heuristic as good-enough;
    any edge case Phase D catches surfaces in §Validation Report.
    """
    statements: list[_LogicalStatement] = []
    buf: list[str] = []
    buf_start: int | None = None

    in_exec_block = False

    for idx, raw_line in enumerate(content.splitlines(), start=1):
        if _is_comment_line(raw_line):
            continue

        # The text from column 8 onwards is COBOL Area A/B (the program
        # text proper). Earlier columns 1-6 are the sequence-number
        # area; column 7 is the indicator. We slice to drop both,
        # falling back to the whole line on short rows.
        text = raw_line[_AREA_A_START:] if len(raw_line) > _AREA_A_START else raw_line.lstrip()
        stripped = text.strip()
        if not stripped:
            # Blank-content lines flush nothing on their own.
            continue

        if buf_start is None:
            buf_start = idx
        buf.append(stripped)

        joined = " ".join(buf)

        # Detect EXEC <CICS|SQL> opening so we know to wait for END-EXEC.
        if not in_exec_block and re.search(r"\bEXEC\s+(CICS|SQL)\b", joined, re.IGNORECASE):
            in_exec_block = True

        if in_exec_block:
            # Terminate on END-EXEC with or without trailing period.
            if re.search(r"\bEND-EXEC\s*\.?\s*$", joined, re.IGNORECASE):
                statements.append(_LogicalStatement(body=joined, start_line=buf_start))
                buf = []
                buf_start = None
                in_exec_block = False
        # COBOL statements terminate on '.' at end of line (the
        # period can also appear mid-statement inside literals, but
        # the simple "line ends with '.'" heuristic is adequate for
        # CALL / COPY recognition — those are short statements).
        elif stripped.endswith("."):
            statements.append(_LogicalStatement(body=joined, start_line=buf_start))
            buf = []
            buf_start = None

    # Flush any trailing buffer (file ended mid-statement).
    if buf and buf_start is not None:
        statements.append(_LogicalStatement(body=" ".join(buf), start_line=buf_start))

    return statements


# ---------------------------------------------------------------------------
# Extractors — one per record type
# ---------------------------------------------------------------------------


def extract_program_calls(
    statements: list[_LogicalStatement],
) -> list[ProgramCallRecord]:
    """Extract LINK / XCTL / CALL edges from pre-processed statements.

    Walks each logical statement and applies the three call-shape regexes
    in priority order (CICS LINK / CICS XCTL / COBOL CALL). A statement
    that matches more than one (rare in real code) records all hits.
    """
    out: list[ProgramCallRecord] = []
    for stmt in statements:
        for match in _EXEC_CICS_LINK_RE.finditer(stmt.body):
            target, resolved = _extract_operand(match)
            out.append(
                ProgramCallRecord(
                    call_kind="link",
                    target=target,
                    resolved=resolved,
                    line=stmt.start_line,
                )
            )
        for match in _EXEC_CICS_XCTL_RE.finditer(stmt.body):
            target, resolved = _extract_operand(match)
            out.append(
                ProgramCallRecord(
                    call_kind="xctl",
                    target=target,
                    resolved=resolved,
                    line=stmt.start_line,
                )
            )
        # CALL is a COBOL statement, not an EXEC CICS one. We skip
        # CALL detection inside EXEC blocks to avoid double-matching
        # "EXEC CICS CALL"-style false positives (no such verb exists,
        # but a regex would otherwise glance at e.g. CALL inside an
        # EXEC SQL CALL-stored-proc statement).
        if re.search(r"\bEXEC\s+(CICS|SQL)\b", stmt.body, re.IGNORECASE):
            continue
        for match in _CALL_RE.finditer(stmt.body):
            target, resolved = _extract_operand(match)
            out.append(
                ProgramCallRecord(
                    call_kind="call_static" if resolved else "call_dynamic",
                    target=target,
                    resolved=resolved,
                    line=stmt.start_line,
                )
            )
    return out


def extract_copybook_includes(
    statements: list[_LogicalStatement],
) -> list[CopybookIncludeRecord]:
    """Extract COPY copybook inclusions from pre-processed statements.

    EXEC SQL INCLUDE statements are surfaced as
    :class:`SqlStatementRecord` (kind ``INCLUDE``), not as COPY records;
    the two have different semantics (host-variable inclusion vs raw
    text substitution).
    """
    out: list[CopybookIncludeRecord] = []
    for stmt in statements:
        # Skip statements that are EXEC blocks — their INCLUDE / COPY
        # semantics differ (EXEC SQL INCLUDE), and we don't want a
        # textual COPY in a multi-line EXEC SQL body to leak into the
        # copybook record list.
        if re.search(r"\bEXEC\s+(CICS|SQL)\b", stmt.body, re.IGNORECASE):
            continue
        for match in _COPY_RE.finditer(stmt.body):
            out.append(
                CopybookIncludeRecord(
                    name=match.group("name"),
                    line=stmt.start_line,
                )
            )
    return out


def extract_vsam_accesses(
    statements: list[_LogicalStatement],
) -> list[VsamAccessRecord]:
    """Extract EXEC CICS READ/WRITE/... file accesses."""
    out: list[VsamAccessRecord] = []
    for stmt in statements:
        for match in _EXEC_CICS_VSAM_RE.finditer(stmt.body):
            file_name, resolved = _extract_operand(match)
            verb = match.group("verb").upper()
            # The verb group is constrained by the alternation to the
            # _VSAM_VERBS list, but mypy can't see that — cast via the
            # tuple membership check.
            assert verb in _VSAM_VERBS
            out.append(
                VsamAccessRecord(
                    file_name=file_name,
                    mode=verb,  # type: ignore[arg-type]
                    resolved=resolved,
                    line=stmt.start_line,
                )
            )
    return out


def extract_sql_statements(
    statements: list[_LogicalStatement],
) -> list[SqlStatementRecord]:
    """Extract embedded EXEC SQL statements (with table-name extraction)."""
    out: list[SqlStatementRecord] = []
    for stmt in statements:
        match = _EXEC_SQL_RE.search(stmt.body)
        if not match:
            continue
        body = match.group("body").strip()
        # Strip the trailing END-EXEC marker so it doesn't pollute the
        # SQL verb extraction.
        body = re.sub(r"\bEND-EXEC\s*\.?\s*$", "", body, flags=re.IGNORECASE).strip()
        kind = _classify_sql_verb(body)
        tables = _extract_sql_tables(body)
        out.append(
            SqlStatementRecord(
                statement_kind=kind,
                tables=tables,
                raw=body,
                line=stmt.start_line,
            )
        )
    return out


def extract_bms_interactions(
    statements: list[_LogicalStatement],
) -> list[BmsInteractionRecord]:
    """Extract EXEC CICS SEND MAP / RECEIVE MAP interactions."""
    out: list[BmsInteractionRecord] = []
    for stmt in statements:
        for match in _EXEC_CICS_BMS_RE.finditer(stmt.body):
            direction = match.group("direction").upper()
            map_name, resolved = _extract_operand(match)
            mapset = _extract_optional_operand(
                match,
                ("mapset_sq", "mapset_dq", "mapset_var"),
            )
            out.append(
                BmsInteractionRecord(
                    direction=direction,  # type: ignore[arg-type]
                    map_name=map_name,
                    mapset=mapset,
                    resolved=resolved,
                    line=stmt.start_line,
                )
            )
    return out


def extract_security_operations(
    statements: list[_LogicalStatement],
) -> list[SecurityOperationRecord]:
    """Extract EXEC CICS SIGNON / EXEC CICS VERIFY PASSWORD operations."""
    out: list[SecurityOperationRecord] = []
    for stmt in statements:
        signon = _EXEC_CICS_SIGNON_RE.search(stmt.body)
        if signon:
            out.append(
                SecurityOperationRecord(
                    operation="SIGNON",
                    userid=_clean_operand(signon.group("userid")),
                    password_ref=_clean_operand(signon.group("password")),
                    line=stmt.start_line,
                )
            )
            # SIGNON and VERIFY are mutually exclusive in a single
            # statement — continue to the next statement.
            continue
        verify = _EXEC_CICS_VERIFY_RE.search(stmt.body)
        if verify:
            out.append(
                SecurityOperationRecord(
                    operation="VERIFY_PASSWORD",
                    userid=_clean_operand(verify.group("userid")),
                    password_ref=_clean_operand(verify.group("password")),
                    line=stmt.start_line,
                )
            )
    return out


def extract_abend_paths(
    statements: list[_LogicalStatement],
) -> list[AbendPathRecord]:
    """Extract EXEC CICS ABEND / HANDLE ABEND error paths.

    HANDLE ABEND is checked *before* ABEND because the former contains
    the verb ``ABEND`` and would otherwise double-match.
    """
    out: list[AbendPathRecord] = []
    for stmt in statements:
        handle_match = _EXEC_CICS_HANDLE_ABEND_RE.search(stmt.body)
        if handle_match:
            code, _ = _maybe_extract_operand(handle_match)
            out.append(
                AbendPathRecord(
                    kind="HANDLE_ABEND",
                    code=code,
                    line=stmt.start_line,
                )
            )
            continue
        abend_match = _EXEC_CICS_ABEND_RE.search(stmt.body)
        if abend_match:
            code, _ = _maybe_extract_operand(abend_match)
            out.append(
                AbendPathRecord(
                    kind="ABEND",
                    code=code,
                    line=stmt.start_line,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Operand helpers
# ---------------------------------------------------------------------------


def _extract_operand(match: re.Match[str]) -> tuple[str, bool]:
    """Pull (operand, resolved) from a regex match with lit_sq/lit_dq/var groups.

    ``resolved`` is True when the source used a quoted literal (the
    target name is the literal). False when the source used a bare
    variable name (resolution requires WORKING-STORAGE cross-reference,
    deferred to the Phase B aggregator).
    """
    lit_sq = match.groupdict().get("lit_sq")
    if lit_sq is not None:
        return lit_sq, True
    lit_dq = match.groupdict().get("lit_dq")
    if lit_dq is not None:
        return lit_dq, True
    var = match.groupdict().get("var")
    if var is not None:
        return var, False
    raise ValueError(
        "Match has no lit_sq / lit_dq / var group — regex is malformed"
    )  # pragma: no cover — regex invariant


def _maybe_extract_operand(match: re.Match[str]) -> tuple[str | None, bool]:
    """Like :func:`_extract_operand` but returns (None, False) if no operand.

    Used by ABEND / HANDLE ABEND where the operand groups are optional.
    """
    gd = match.groupdict()
    if gd.get("lit_sq") is not None:
        return gd["lit_sq"], True
    if gd.get("lit_dq") is not None:
        return gd["lit_dq"], True
    if gd.get("var") is not None:
        return gd["var"], False
    return None, False


def _extract_optional_operand(
    match: re.Match[str],
    group_names: tuple[str, str, str],
) -> str | None:
    """Pull an optional operand value across a (sq, dq, var) group triplet."""
    gd = match.groupdict()
    sq, dq, var = group_names
    if gd.get(sq) is not None:
        return gd[sq]
    if gd.get(dq) is not None:
        return gd[dq]
    if gd.get(var) is not None:
        return gd[var]
    return None


def _clean_operand(operand: str | None) -> str | None:
    """Trim whitespace + outer quotes from an operand.

    SIGNON / VERIFY operands are captured loosely (everything up to the
    closing ``)``). We trim incidental whitespace + outer quote chars
    so downstream consumers see clean field names.
    """
    if operand is None:
        return None
    cleaned = operand.strip()
    if not cleaned:
        return None
    if (cleaned.startswith("'") and cleaned.endswith("'")) or (
        cleaned.startswith('"') and cleaned.endswith('"')
    ):
        cleaned = cleaned[1:-1]
    return cleaned or None


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------


def _classify_sql_verb(
    body: str,
) -> Literal[
    "SELECT",
    "UPDATE",
    "INSERT",
    "DELETE",
    "DECLARE",
    "OPEN",
    "FETCH",
    "CLOSE",
    "INCLUDE",
    "BEGIN",
    "OTHER",
]:
    """Classify an EXEC SQL body by its leading verb.

    The match is case-insensitive on a leading word; anything unknown
    falls through to ``OTHER`` (which the Phase B aggregator can flag
    for manual review).
    """
    first_word_match = re.match(r"\s*([A-Za-z]+)", body)
    if not first_word_match:
        return "OTHER"
    verb = first_word_match.group(1).upper()
    if verb in {
        "SELECT",
        "UPDATE",
        "INSERT",
        "DELETE",
        "DECLARE",
        "OPEN",
        "FETCH",
        "CLOSE",
        "INCLUDE",
        "BEGIN",
    }:
        return verb  # type: ignore[return-value]
    return "OTHER"


def _extract_sql_tables(body: str) -> tuple[str, ...]:
    """Extract a sorted, deduplicated tuple of table names from a SQL body."""
    seen: set[str] = set()
    for match in _SQL_TABLE_RE.finditer(body):
        seen.add(match.group("table"))
    return tuple(sorted(seen))


# ---------------------------------------------------------------------------
# Public entry — per-file parser
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseResult:
    """The aggregate output of :func:`parse_cobol_source` for one file.

    Kept as a frozen dataclass — Phase B can dict-coerce it for JSON
    serialisation but most consumers (the aggregator + the unit tests)
    work against the attribute API.
    """

    path: Path
    program_calls: tuple[ProgramCallRecord, ...] = field(default_factory=tuple)
    copybook_includes: tuple[CopybookIncludeRecord, ...] = field(default_factory=tuple)
    vsam_accesses: tuple[VsamAccessRecord, ...] = field(default_factory=tuple)
    sql_statements: tuple[SqlStatementRecord, ...] = field(default_factory=tuple)
    bms_interactions: tuple[BmsInteractionRecord, ...] = field(default_factory=tuple)
    security_operations: tuple[SecurityOperationRecord, ...] = field(
        default_factory=tuple,
    )
    abend_paths: tuple[AbendPathRecord, ...] = field(default_factory=tuple)


_CATEGORY_KEYS: Final[tuple[str, ...]] = (
    "program_calls",
    "copybook_includes",
    "vsam_accesses",
    "sql_statements",
    "bms_interactions",
    "security_operations",
    "abend_paths",
)


#: Lowercased file extensions Phase A's auto-detect treats as
#: CICS / COBOL-shaped signal. These mirror the extensions in
#: :data:`primitives._shared.discovery.recon._EXTENSION_TO_LANGUAGE`
#: that classify into the ``cobol`` / ``copybook`` / ``jcl`` buckets —
#: see the auto-detect heuristic table in
#: ``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``.
_COBOL_EXTENSIONS: Final[frozenset[str]] = frozenset({".cbl", ".cob", ".cpy", ".copy"})
_JCL_EXTENSIONS: Final[frozenset[str]] = frozenset({".jcl"})


#: How many ``*.cbl`` / ``*.cob`` files the detector samples for the
#: ``EXEC CICS`` corroboration probe. The sample is *not* the whole tree
#: — auto-detect is meant to be cheap; a handful of files is enough to
#: corroborate a CICS-shaped repo without paying the I/O cost of a full
#: walk. (Phase A's deep walk pays that cost later, once the flavor is
#: settled.)
_DETECT_CICS_PROBE_LIMIT: Final[int] = 8


def detect(code_root: Path) -> Confidence:
    """Auto-detect the confidence that ``code_root`` is a CICS-COBOL tree.

    Implements the row of the auto-detect heuristic table at
    ``docs/superpowers/specs/2026-06-03-polyglot-discovery-design.md``
    §"Auto-detection heuristic" that reads:

    * ``*.cbl`` + ``*.jcl`` + ``EXEC CICS`` strings → ``high``
    * ``*.cbl`` / ``*.cob`` / ``*.cpy`` / ``*.copy`` present without the
      CICS-statement corroboration → ``medium``
    * none of the above → ``low``

    The walk is depth-capped (sibling of the Phase A walker invariant
    — see :mod:`primitives._shared.discovery.recon`): symlinks
    are not followed and unreadable files log + skip rather than abort.
    Built so a sibling flavor pack's ``detect()`` call against the same
    ``code_root`` does not amplify into a quadratic walk: the depth-cap
    keeps the cost ``O(files_at_top_N_levels)`` per pack.

    Args:
        code_root: Directory the dispatcher is considering routing to
            the ``cics_cobol`` flavor. Phase A's
            :func:`~primitives._shared.discovery.recon.detect_platform_flavor`
            calls every registered flavor's ``detect()`` and picks the
            highest confidence (lexicographic tiebreak; see #525 / the
            spec §"Conflict resolution").

    Returns:
        ``"high"`` / ``"medium"`` / ``"low"`` per the table above. The
        result feeds the closed :data:`Confidence` Literal so the
        ReconReport can persist it without schema gymnastics.
    """
    if not code_root.exists() or not code_root.is_dir():
        return "low"

    has_cobol = False
    has_jcl = False
    cobol_candidates: list[Path] = []

    # Walk the whole tree but bail out early once the strongest signal
    # is corroborated. Symlinks are skipped to match the Phase A walker
    # invariant. Unreadable directories log + skip.
    try:
        iterator = code_root.rglob("*")
    except OSError:
        return "low"

    for entry in iterator:
        try:
            if entry.is_symlink() or not entry.is_file():
                continue
        except OSError:
            continue
        suffix = entry.suffix.lower()
        if suffix in _COBOL_EXTENSIONS:
            has_cobol = True
            if suffix in {".cbl", ".cob"} and len(cobol_candidates) < _DETECT_CICS_PROBE_LIMIT:
                cobol_candidates.append(entry)
        elif suffix in _JCL_EXTENSIONS:
            has_jcl = True
        # Early-out: once both signals are set and we have enough
        # cobol probe candidates, no point continuing the walk.
        if has_cobol and has_jcl and len(cobol_candidates) >= _DETECT_CICS_PROBE_LIMIT:
            break

    if not has_cobol:
        return "low"

    # CICS corroboration probe: any of the cobol candidates contains an
    # ``EXEC CICS`` statement → high (paired with the JCL signal).
    has_exec_cics = False
    for candidate in cobol_candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Case-insensitive: dialects accept lower-case keywords. The
        # match is intentionally substring (not regex-anchored) — false
        # positives from a string literal that contains "EXEC CICS" are
        # still strong signal that this is a CICS-shaped codebase.
        if "EXEC CICS" in text.upper():
            has_exec_cics = True
            break

    if has_jcl and has_exec_cics:
        return "high"
    if has_cobol:
        # ``.cbl`` / ``.cob`` / ``.cpy`` / ``.copy`` present without the
        # JCL+CICS corroboration combo: still COBOL-shaped, just not
        # the canonical CICS/COBOL transaction-processing pattern.
        return "medium"
    return "low"


def parse_cobol_source(
    path: Path,
    content: str,
) -> dict[str, list[object]]:
    """Parse one COBOL source file's content into category-keyed lists.

    This is the public per-file entry point the Phase B aggregator
    (``shared/analyze.py``, lands in M5-ANALYZE-1) drives. Returns a
    dict keyed by the seven record category names (see
    :data:`_CATEGORY_KEYS`), each mapping to a list of extracted
    records.

    The dict-of-lists shape mirrors the M1 dialect-pack interface so
    Phase B's aggregator can apply uniform downstream handling
    regardless of which flavor extracted the records.

    Args:
        path: Source file path (used by Phase B to set
            ``ProgramRecord.source_path``; not consulted by the parser).
        content: Raw file content (the aggregator reads + decodes the
            file before calling here, so we accept ``str`` not bytes).

    Returns:
        A dict with keys matching :data:`_CATEGORY_KEYS`. Each value is
        a list of the corresponding record type (frozen dataclass
        instances).
    """
    statements = _iter_statements(content)
    result = ParseResult(
        path=path,
        program_calls=tuple(extract_program_calls(statements)),
        copybook_includes=tuple(extract_copybook_includes(statements)),
        vsam_accesses=tuple(extract_vsam_accesses(statements)),
        sql_statements=tuple(extract_sql_statements(statements)),
        bms_interactions=tuple(extract_bms_interactions(statements)),
        security_operations=tuple(extract_security_operations(statements)),
        abend_paths=tuple(extract_abend_paths(statements)),
    )
    return {
        "program_calls": list(result.program_calls),
        "copybook_includes": list(result.copybook_includes),
        "vsam_accesses": list(result.vsam_accesses),
        "sql_statements": list(result.sql_statements),
        "bms_interactions": list(result.bms_interactions),
        "security_operations": list(result.security_operations),
        "abend_paths": list(result.abend_paths),
    }


__all__ = [
    "AbendPathRecord",
    "BmsInteractionRecord",
    "CopybookIncludeRecord",
    "ParseResult",
    "ProgramCallRecord",
    "SecurityOperationRecord",
    "SqlStatementRecord",
    "VsamAccessMode",
    "VsamAccessRecord",
    "detect",
    "extract_abend_paths",
    "extract_bms_interactions",
    "extract_copybook_includes",
    "extract_program_calls",
    "extract_security_operations",
    "extract_sql_statements",
    "extract_vsam_accesses",
    "parse_cobol_source",
]

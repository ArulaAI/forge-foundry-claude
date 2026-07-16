"""M2 Stage D — ``detect_side_effects`` (email / file / external / dynamic SQL surfaces).

Walks a Stage A :class:`ParsedSP` and returns the
:data:`primitives.stored_proc_analyze.shared.models.SideEffect` literals
present in the body. The four non-``none`` surfaces map to the package /
DBMS / pipeline mechanisms the M-tier conversion strategist needs to
plan around:

* ``email`` — Oracle ``DBMS_SMTP`` / ``UTL_MAIL`` / ``UTL_SMTP``; T-SQL
  ``sp_send_dbmail`` / ``sp_notify_operator``. DB2 has no equivalent
  built-in; CDC/external-mail patterns surface as ``external``.
* ``file`` — Oracle ``UTL_FILE`` (``FOPEN`` / ``PUT_LINE`` / ``FCLOSE``);
  T-SQL ``OPENROWSET BULK`` / ``BULK INSERT`` and the deprecated file
  cmdshell forms. DB2 ``IMPORT`` / ``EXPORT`` invoked via ``CALL`` also
  count; ``UTL_FILE`` matches verbatim across the Oracle / DB2 register.
* ``external`` — Oracle ``DBMS_PIPE`` / ``DBMS_AQ`` / ``DBMS_ALERT`` /
  ``UTL_HTTP`` / ``UTL_TCP`` / ``UTL_URL``; T-SQL ``xp_cmdshell``,
  Service Broker (``SEND`` ON CONVERSATION``, ``RECEIVE``), and the
  ``CLR`` external-procedure form. DB2 MQ functions (``MQSEND``,
  ``MQREAD``, ``MQRECEIVE``, ``MQPUBLISH``) and ``SYSPROC.HTTPGETBLOB`` /
  ``SYSPROC.HTTPGETCLOB``.
* ``dynamic_sql`` — Oracle ``EXECUTE IMMEDIATE`` (and the
  ``DBMS_SQL.PARSE`` / ``DBMS_SQL.EXECUTE`` package form); T-SQL
  ``sp_executesql``, ``EXEC(@sql)``, ``EXECUTE(@sql)`` (the string-form
  variants, distinct from sub-procedure invocation handled by Stage B);
  DB2 ``PREPARE <stmt> FROM`` / ``EXECUTE IMMEDIATE``.

Why regex against ``raw_body`` (not pure AST walking): sqlglot 30.x
collapses most of the dialect-specific package / pragma calls (e.g.
``DBMS_SMTP.MAIL``, ``sp_send_dbmail``, ``EXEC(@sql)``) into either
:class:`exp.Command` fallbacks (with the package name buried in the
expression payload) or unparseable nodes. The AST-first approach would
miss the long tail of vendor pragmas this stage exists to surface.
Regex over a comment-stripped body is dialect-portable, deterministic,
and resilient to graceful-degradation parses where ``ast`` is ``None``
(issue #205 contract).

Return shape:

* Always a sorted list with no duplicates, drawn from the
  :data:`SideEffect` Literal vocabulary.
* The literal ``"none"`` is returned when no surface fires **for a
  parseable SP**. It is a positive signal ("we looked and found
  nothing") rather than a placeholder, so downstream M3 sequencing and
  M4 plan-synthesis can branch on ``"none" in sp.side_effects`` without
  nullable reasoning. An *unparseable* SP with no surface returns
  ``"unknown"`` instead (#1343) — "we could not fully look", not "we
  found nothing" — so a write-heavy proc the parser choked on is never
  labelled side-effect-free.

Design spec §M2 Stage D
(``docs/superpowers/specs/2026-05-22-data-modernization-design.md``).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sqlglot import exp

if TYPE_CHECKING:
    from primitives.stored_proc_analyze.shared.models import SideEffect
    from primitives.stored_proc_analyze.shared.sp_parser import ParsedSP


# ---------------------------------------------------------------------------
# Comment-stripping primitives
# ---------------------------------------------------------------------------

# Mirror the regex pair used by Stage B / Stage C — block comments are
# erased first so a side-effect token inside ``/* ... */`` cannot trigger
# a false positive; then line-comment-only lines are filtered out so
# ``-- EXEC sp_executesql`` in a header comment does not light up the
# dynamic-SQL detector.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_ONLY_RE = re.compile(r"^\s*--")

# String-literal stripper used so a package mention embedded inside a
# quoted string (``'see DBMS_SMTP.MAIL example'``) does not trigger the
# detector. Handles both single-quoted SQL strings and the T-SQL N''
# Unicode form. We use the simple ``'(?:''|[^'])*'`` pattern that copes
# with embedded ``''`` doubled-quote escapes — adequate for the legacy
# DDL the M-tier targets without dragging in a full SQL lexer.
_STRING_LITERAL_RE = re.compile(r"N?'(?:''|[^'])*'", re.IGNORECASE)


def _strip_for_scan(raw_body: str) -> str:
    """Return ``raw_body`` with comments and string literals erased.

    The detector regexes that follow run on this cleaned text so a
    package name mentioned inside ``/* note */``, ``-- todo`` or
    ``'docs: UTL_FILE'`` does not produce a false positive. We replace
    matches with whitespace (rather than the empty string) so positions
    stay aligned for any downstream debugging — the side-effect
    classifier does not surface offsets today, but the alignment makes
    iterative debugging painless when it eventually does.
    """
    if not raw_body:
        return ""
    no_blocks = _BLOCK_COMMENT_RE.sub(lambda m: " " * len(m.group(0)), raw_body)
    no_strings = _STRING_LITERAL_RE.sub(lambda m: " " * len(m.group(0)), no_blocks)
    return "\n".join(
        line for line in no_strings.splitlines() if not _LINE_COMMENT_ONLY_RE.match(line)
    )


# ---------------------------------------------------------------------------
# Per-surface detector regexes
# ---------------------------------------------------------------------------

# Email surfaces. Oracle ``DBMS_SMTP`` / ``UTL_MAIL`` / ``UTL_SMTP`` are
# the standard XMLDB / Apex mail packages. T-SQL ``sp_send_dbmail`` and
# ``sp_notify_operator`` ship with the SQL Server Database Mail subsystem.
# ``\b`` boundaries keep ``SP_SEND_DBMAIL_AUDIT`` (a hypothetical wrapper)
# from triggering on the substring match — the legacy convention is to
# call the built-in directly, so the strict boundary form is correct.
_EMAIL_RE = re.compile(
    r"\b("
    r"DBMS_SMTP"
    r"|UTL_MAIL"
    r"|UTL_SMTP"
    r"|sp_send_dbmail"
    r"|sp_notify_operator"
    r")\b",
    re.IGNORECASE,
)

# File surfaces. ``UTL_FILE`` covers Oracle + DB2 file I/O via the
# ``FOPEN`` / ``PUT_LINE`` / ``FCLOSE`` / ``GET_LINE`` calls. ``BULK
# INSERT`` and ``OPENROWSET(BULK ...)`` are T-SQL file ingestion forms.
# The ``BULK`` matcher requires the following ``INSERT`` keyword (with
# optional whitespace) so a column literally named ``BULK`` cannot
# misfire.
_FILE_RE = re.compile(
    r"\b("
    r"UTL_FILE"
    r"|BULK\s+INSERT"
    r"|OPENROWSET\s*\(\s*BULK"
    r")\b",
    re.IGNORECASE,
)

# External surfaces. Oracle: ``DBMS_PIPE`` (inter-session messaging),
# ``DBMS_AQ`` (Advanced Queueing — JMS bridge), ``DBMS_ALERT`` (signal
# fan-out), and the ``UTL_HTTP`` / ``UTL_TCP`` / ``UTL_URL`` HTTP /
# socket clients. T-SQL: ``xp_cmdshell`` (shell-out), Service Broker
# ``SEND ON CONVERSATION`` / ``RECEIVE`` / ``BEGIN DIALOG``, and CLR
# external procedures (``EXTERNAL NAME``). DB2: MQ functions
# (``MQSEND``, ``MQREAD``, ``MQRECEIVE``, ``MQPUBLISH``) and
# ``HTTPGETBLOB`` / ``HTTPGETCLOB`` from ``SYSPROC``. Listed broadly
# enough that a real legacy SP touching any inter-process boundary is
# tagged; the cost of a false positive here is "M3 schedules an
# adapter conversation that turns out unnecessary" — bounded.
_EXTERNAL_RE = re.compile(
    r"\b("
    r"DBMS_PIPE"
    r"|DBMS_AQ"
    r"|DBMS_ALERT"
    r"|UTL_HTTP"
    r"|UTL_TCP"
    r"|UTL_URL"
    r"|xp_cmdshell"
    r"|SEND\s+ON\s+CONVERSATION"
    r"|BEGIN\s+DIALOG\s+CONVERSATION"
    r"|EXTERNAL\s+NAME"
    r"|MQSEND"
    r"|MQREAD"
    r"|MQRECEIVE"
    r"|MQPUBLISH"
    r"|HTTPGETBLOB"
    r"|HTTPGETCLOB"
    r")\b",
    re.IGNORECASE,
)

# Service Broker ``RECEIVE`` is more permissive: the verb can stand
# alone or appear as ``WAITFOR (RECEIVE ...)``. We match either the
# ``WAITFOR (RECEIVE`` form or a bare ``RECEIVE TOP`` (the canonical
# Service Broker receive prelude). A plain ``RECEIVE`` keyword would
# false-positive on too many things, so we require one of these two
# disambiguators.
_SERVICE_BROKER_RECEIVE_RE = re.compile(
    r"\b("
    r"WAITFOR\s*\(\s*RECEIVE"
    r"|RECEIVE\s+TOP"
    r")\b",
    re.IGNORECASE,
)

# Dynamic SQL surfaces. Oracle: ``EXECUTE IMMEDIATE`` and the
# ``DBMS_SQL`` package (``PARSE``, ``EXECUTE``, ``BIND_VARIABLE``,
# ``OPEN_CURSOR``). T-SQL: ``sp_executesql`` and the string-form
# ``EXEC(@var)`` / ``EXECUTE(@var)`` variants. DB2: ``PREPARE`` … ``FROM``
# and ``EXECUTE IMMEDIATE``. ``EXEC(`` and ``EXECUTE(`` matches the
# parenthesised string form distinct from ``EXEC proc_name`` —
# Stage B's out-call counter handles the sub-procedure call form.
_DYNAMIC_SQL_RE = re.compile(
    r"("
    r"\bEXECUTE\s+IMMEDIATE\b"
    r"|\bDBMS_SQL\b"
    r"|\bsp_executesql\b"
    r"|\bEXEC\s*\("
    r"|\bEXECUTE\s*\("
    r"|\bPREPARE\b[^\n;]{0,200}\bFROM\b"
    r")",
    re.IGNORECASE,
)


# Audit / log / notification / outbox / history write surfaces (#1293). A write
# (INSERT/UPDATE/DELETE/MERGE) into a table of this shape is an ``audit_write``
# side effect: the migrator must preserve the audit-trail / outbox write
# semantics transactionally across the strangler boundary — a data-integrity
# hazard distinct from a plain domain-table write. The alternatives are anchored
# on word-ish boundaries (start/end of the name, or an ``_`` separator) so
# ``catalog`` / ``blog`` / ``dialog`` do not match on a bare ``log`` substring.
_AUDIT_TABLE_RE = re.compile(
    r"(?:^|_)("
    r"audit"
    r"|log|logs"
    r"|notification|notifications"
    r"|outbox"
    r"|eventlog|event_log"
    r"|changelog|change_log"
    r"|history|hist"
    r")(?:$|_)",
    re.IGNORECASE,
)


def _write_target_tables(ast: exp.Expression) -> list[str]:
    """Return the unqualified names of every INSERT/UPDATE/DELETE/MERGE target."""
    names: list[str] = []
    for node in ast.find_all(exp.Insert, exp.Update, exp.Delete, exp.Merge):
        target = node.this
        # INSERT wraps its target in a Schema (table + column list); UPDATE /
        # DELETE / MERGE carry the Table directly.
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table):
            names.append(target.name)
    return names


def audit_write_tables(parsed_sp: ParsedSP) -> tuple[str, ...]:
    """Return the sorted distinct audit-shaped tables the SP WRITES to (#1293).

    A table is audit-shaped when its unqualified name matches
    :data:`_AUDIT_TABLE_RE` (audit / log / notification / outbox / event-log /
    changelog / history). Only *write* targets count — reading an audit table
    is not an ``audit_write`` side effect. Returns ``()`` for an unparseable SP
    (``ast is None``), since write targets cannot be reliably recovered without
    the AST.
    """
    ast = parsed_sp.ast
    if ast is None:
        return ()
    audit = {name for name in _write_target_tables(ast) if _AUDIT_TABLE_RE.search(name)}
    return tuple(sorted(audit))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def detect_side_effects(parsed_sp: ParsedSP) -> list[SideEffect]:
    """Return the sorted list of :data:`SideEffect` literals present in ``parsed_sp``.

    Args:
        parsed_sp: A Stage A :class:`ParsedSP` (parsed *or* unparseable
            — Stage D operates on ``raw_body`` and works in both
            cases). Unparseable bodies still get a fair scan because
            the raw text is preserved.

    Returns:
        A sorted list drawn from the closed
        :data:`primitives.stored_proc_analyze.shared.models.SideEffect`
        vocabulary. When no surface is detected: a *parseable* SP returns
        ``["none"]`` (a real "no side effects" fact); an *unparseable* SP
        returns ``["unknown"]`` (#1343) because the absent AST means the
        scan was incomplete. Neither ``"none"`` nor ``"unknown"`` ever
        appears alongside a detected surface. Duplicates are removed;
        ordering is alphabetic so two SPs with the same surface set always
        compare equal.
    """
    cleaned = _strip_for_scan(parsed_sp.raw_body)

    detected: set[SideEffect] = set()
    if _has_email(cleaned):
        detected.add("email")
    if _has_file(cleaned):
        detected.add("file")
    if _has_external(cleaned):
        detected.add("external")
    if _has_dynamic_sql(cleaned):
        detected.add("dynamic_sql")
    # audit_write keys off the AST write targets, not the comment-stripped text.
    if audit_write_tables(parsed_sp):
        detected.add("audit_write")

    if not detected:
        # An *unparseable* SP degrades to ``["unknown"]`` rather than
        # ``["none"]`` (#1343): the raw-body scan can only surface the
        # regex-detectable mechanisms (email/file/external/dynamic_sql),
        # while ``audit_write`` — and any write the regexes don't model —
        # relies on the AST that is absent here. Claiming "no side effects"
        # for a proc we could not fully analyze is a silent-failure lie.
        # A *parseable* SP with nothing detected is a genuine ``["none"]``.
        if parsed_sp.parse_status == "unparseable":
            return ["unknown"]
        return ["none"]
    return sorted(detected)


# ---------------------------------------------------------------------------
# Detector helpers (one per surface)
# ---------------------------------------------------------------------------


def _has_email(cleaned: str) -> bool:
    """Return ``True`` when an email side-effect mechanism is present."""
    return bool(_EMAIL_RE.search(cleaned))


def _has_file(cleaned: str) -> bool:
    """Return ``True`` when a file-system side-effect mechanism is present."""
    return bool(_FILE_RE.search(cleaned))


def _has_external(cleaned: str) -> bool:
    """Return ``True`` when an external (queue / HTTP / shell / CLR) surface is present.

    Service Broker ``RECEIVE`` is checked separately because the verb
    on its own is too ambiguous; we require either the ``WAITFOR
    (RECEIVE`` form or the ``RECEIVE TOP`` prelude.
    """
    if _EXTERNAL_RE.search(cleaned):
        return True
    return bool(_SERVICE_BROKER_RECEIVE_RE.search(cleaned))


def _has_dynamic_sql(cleaned: str) -> bool:
    """Return ``True`` when a dynamic-SQL execution form is present."""
    return bool(_DYNAMIC_SQL_RE.search(cleaned))


__all__ = ["audit_write_tables", "detect_side_effects"]

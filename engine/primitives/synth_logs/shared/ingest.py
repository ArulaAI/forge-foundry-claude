"""M6 ``/mod-synth-logs`` Phase A — playbook + codebase ingest.

Phase A produces the closed ``EntitiesDigest`` Phase B / C / D consume
as the ground-truth catalog. Two paths, one output schema:

* **Path 1 — ``ingest_from_playbook``** parses an M5-produced
  ``PLAYBOOK.md`` for §3 Program Inventory + §5 Call Chain Maps + §6
  Data Architecture + §7 Screen Navigation + §9 Batch Processing +
  §11 Deployment Architecture.
* **Path 2 — ``ingest_from_codebase``** is the fallback the M6 CLI
  walks when ``--playbook`` is absent. It re-uses M5's Phase B
  regex/extract pipeline (``run_recon`` + ``build_program_inventory``)
  via a dependency-injected callable so M6 tests can drive the
  projection without spinning up a full M5 walk, and so a future T2
  orchestrator can substitute a smarter analyzer without touching this
  module. Both paths emit the same ``EntitiesDigest`` shape so
  downstream phases stay agnostic (design spec §"Pipeline", Phase A
  row).

Design spec: ``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``

Sections consumed (per the M6-INGEST-1 contract):

* **§3 Program Inventory** — COBOL / PL/I program names (controller,
  business, data, security, utility, ATM subsystem, batch, PL/I variants)
  → ``EntitiesDigest.programs``
* **§5 Call Chain Maps** — program-to-program edges harvested from the
  *Evidence* tables (``BBANK10P → DBANK01P`` etc). These are folded back
  into ``programs`` (the closed universe) so Phase B's cascading-failure
  composer can walk real call edges; the edge list itself is *not* a
  field on ``EntitiesDigest`` because the digest is a normalised name
  set (design spec §"Phase A" final paragraph).
* **§6 Data Architecture** — VSAM file inventory, copybook record
  layouts, SQL table variants → ``EntitiesDigest.files`` +
  ``EntitiesDigest.copybooks``
* **§7 Screen Navigation & UI Layer** — BMS map inventory, transaction
  identifiers (``BANK`` from the screen-flow diagram, BMS map names like
  ``BANK10A``) → ``EntitiesDigest.transactions``
* **§9 Batch Processing** — JCL job inventory → folded into
  ``EntitiesDigest.programs`` (JCL jobs are *programs* from M6's
  perspective — Phase C's ``batch_overrun`` event kind needs them in
  the closed name set)
* **§11 Deployment Architecture** — hostnames, IPs, port numbers,
  region names → ``EntitiesDigest.networks_and_ports``

**Graceful degradation**: missing sections produce empty lists, not
exceptions. The function returns a valid ``EntitiesDigest`` even when
fed a near-empty playbook so that an unfinished discovery run does not
break the M6 pipeline; Phase D's content-accuracy rule is what fails
loudly if entities referenced by emitted logs cannot resolve here.

The parser is **pure-Python regex** — no Markdown AST library — both to
keep the runtime dependency footprint minimal and so we can fail soft
on the irregular hand-edited content the spec allows (design spec
§"File contracts", row ``user → M6``: *resilient to hand-edits within
the 16-section structure*).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Final

from primitives._shared.discovery import (
    build_program_inventory,
    run_recon,
)
from primitives.synth_logs.shared.models import EntitiesDigest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from primitives._shared.discovery import (
        CopybookDag,
        CopybookRecord,
        MainframeFlavor,
        ProgramRecord,
    )

    AnalyzeImpl = Callable[
        [Path, MainframeFlavor],
        tuple[list[ProgramRecord], list[CopybookRecord], CopybookDag],
    ]
    """Type alias for the ``analyze_impl`` dependency-injection callable.

    The callable receives ``(code_root, mainframe_flavor)`` and returns
    the M5-shaped ``(programs, copybooks, dag)`` tuple
    :func:`primitives._shared.discovery.build_program_inventory` emits.
    Tests pass a stub here to assert the projection without running
    the full M5 walk; production callers leave the parameter ``None``
    so the default delegates to M5's ``run_recon`` +
    ``build_program_inventory`` chain.
    """

# ---------------------------------------------------------------------------
# Section anchor matching
# ---------------------------------------------------------------------------
#
# The 16-section playbook structure (design spec §"Output contract") uses
# ``## N. Title`` for top-level sections. We capture the body between an
# anchor and the next ``## `` to scope each section's parse independently.

_SECTION_RE: Final = re.compile(
    r"^## (?P<num>\d+)\.\s+(?P<title>.+?)\s*$",
    re.MULTILINE,
)

# Section numbers we care about. Title is informational — we anchor on the
# leading number so a rename ("§6 Data" -> "§6 Data Architecture") does not
# break the parser, and so a hand-edited playbook that fixes a typo in a
# header still ingests cleanly.
_REQUIRED_SECTIONS: Final[dict[int, str]] = {
    3: "Program Inventory",
    5: "Call Chain Maps",
    6: "Data Architecture",
    7: "Screen Navigation & UI Layer",
    9: "Batch Processing",
    11: "Deployment Architecture",
}


# ---------------------------------------------------------------------------
# Bold-token extraction
# ---------------------------------------------------------------------------
#
# Most playbook tables put the entity name as the first column wrapped in
# ``**NAME**``. We extract the bolded token directly — the surrounding
# table markup (``|`` cells, header rows) is irrelevant to the closed
# name set we feed downstream phases.

_BOLD_TOKEN_RE: Final = re.compile(r"\*\*([A-Z][A-Z0-9_\-]{1,30})\*\*")
"""Match a bold-wrapped uppercase identifier of 2..31 chars.

We intentionally constrain to ``[A-Z][A-Z0-9_\\-]`` because every
mainframe artefact name in the BankDemo reference (program names like
``BBANK10P``, copybooks ``CBANKVCS``, VSAM files ``BNKACC``, JCL jobs
``ZBNKEXT1``, BMS maps ``BANK10A``) is uppercase. This both filters out
prose words bolded for emphasis (e.g. ``**Note**:``) and short tokens
like ``**X**`` that would otherwise pollute the name set.
"""


# ---------------------------------------------------------------------------
# Copybook reference extraction (§6 prose + code-fence headers)
# ---------------------------------------------------------------------------

_COPYBOOK_HEADER_RE: Final = re.compile(
    r"^####\s+(?P<name>C[A-Z0-9]{4,15})\s+—",
    re.MULTILINE,
)
"""§6.3 declares each copybook record layout under an ``#### NAME — ...``
heading (e.g. ``#### CBANKVCS — Customer Record (250 bytes)``). The
copybook naming convention in the BankDemo reference is ``C`` + 4-15
uppercase alphanumerics.
"""

_COPYBOOK_PATH_RE: Final = re.compile(
    r"sources/copybook/(?P<name>[A-Z][A-Z0-9_]{1,30})\.cpy",
)
"""Catch copybook references like ``sources/copybook/CBANKVCS.cpy:25-41``
that appear in evidence blocks and prose citations.
"""


# ---------------------------------------------------------------------------
# Network identifiers (§11)
# ---------------------------------------------------------------------------

_IP_RE: Final = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?\b")
_HOSTNAME_RE: Final = re.compile(
    # Look for ``host: <name>``-style declarations or backticked hostnames
    # ending in a typical mainframe-runtime suffix (.local, .corp, etc).
    r"`([a-zA-Z][a-zA-Z0-9_\-]{2,40}(?:\.[a-zA-Z][a-zA-Z0-9_\-]{1,30}){1,4})`",
)
_PORT_RE: Final = re.compile(
    # ``port 12345``, ``:12345`` after host, or ``Port: 12345`` in tables.
    r"\b(?:port|listener|listening on)\s*[:#]?\s*(\d{2,5})\b",
    re.IGNORECASE,
)
_REGION_NAME_RE: Final = re.compile(
    # §11.1 table: region names appear as bold tokens (``**BANKVSAM**``,
    # ``**BANKSQL**``, etc). They're also valid network identifiers from
    # the synth-logs perspective because the enterprise-server log writes
    # the region name into every line.
    r"\*\*(BANK[A-Z]{2,15})\*\*",
)


# ---------------------------------------------------------------------------
# Transaction identifiers
# ---------------------------------------------------------------------------

_TRANSACTION_FROM_TEXT_RE: Final = re.compile(
    # CICS transactions are typically 4-character codes (``BANK``, ``CESN``,
    # ``CEMT``). The screen-flow diagram (§7.1) annotates the entry point
    # with ``(Transaction BANK)``.
    r"\(Transaction\s+([A-Z][A-Z0-9]{1,7})\)",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ingest_from_playbook(playbook_path: Path) -> EntitiesDigest:
    """Parse a PLAYBOOK.md and return the corresponding ``EntitiesDigest``.

    Args:
        playbook_path: Path to an M5-produced (or hand-authored)
            ``PLAYBOOK.md``. The file is read once into memory; the
            91 KB BankDemo exemplar parses in < 200 ms.

    Returns:
        ``EntitiesDigest`` populated from the playbook. Sections that are
        absent or empty contribute zero entries to the digest — the call
        does *not* raise. ``schema_version`` is fixed at ``"1.0"`` by
        the model; ``generated_at`` is stamped on construction.

    Raises:
        FileNotFoundError: ``playbook_path`` does not exist.
        OSError: read failed (permissions, encoding, etc).

    Example:
        >>> from pathlib import Path
        >>> digest = ingest_from_playbook(Path("PLAYBOOK.md"))
        >>> "BBANK10P" in digest.programs
        True
    """
    text = playbook_path.read_text(encoding="utf-8")
    sections = _split_into_sections(text)

    programs = _parse_programs(sections.get(3, ""))
    programs |= _parse_call_chain_programs(sections.get(5, ""))
    programs |= _parse_jcl_jobs(sections.get(9, ""))

    files = _parse_files(sections.get(6, ""))
    copybooks = _parse_copybooks(sections.get(6, ""))
    transactions = _parse_transactions(sections.get(7, ""))
    networks_and_ports = _parse_deployment(sections.get(11, ""))

    return EntitiesDigest(
        programs=sorted(programs),
        files=sorted(files),
        copybooks=sorted(copybooks),
        transactions=sorted(transactions),
        networks_and_ports=sorted(networks_and_ports),
    )


def ingest_from_codebase(
    code_root: Path,
    *,
    mainframe_flavor: MainframeFlavor = "cics_cobol",
    analyze_impl: AnalyzeImpl | None = None,
) -> EntitiesDigest:
    """Walk ``code_root`` directly and return an ``EntitiesDigest``.

    Path 2 of M6 Phase A — the codebase-fallback the CLI takes when no
    ``--playbook`` resolves. Re-uses M5's Phase B analyze chain
    (:func:`primitives._shared.discovery.run_recon` +
    :func:`primitives._shared.discovery.build_program_inventory`)
    through a dependency-injected callable so tests can substitute a
    stub and so a future T2 orchestrator can plug in a richer analyzer
    without touching M6.

    The projection is *deliberately minimal*: ``ProgramRecord.name`` →
    ``EntitiesDigest.programs`` and ``CopybookRecord.name`` →
    ``EntitiesDigest.copybooks``. Unresolved ``calls_to`` targets
    (programs the static walk could not bind to a source file) are
    folded into ``programs`` too so Phase B's cascading-incident
    composer can walk real call-chain edges — the same widening the
    playbook path does for §5 Call Chain Maps targets. The other
    digest fields (``files``, ``transactions``, ``networks_and_ports``)
    stay empty until M5-ANALYZE-2 (#449) extracts the CSD / BMS / JCL
    matrices; Phase D's content-accuracy rule will surface log entries
    that reference missing entities so downstream consumers see the
    gap rather than silently shipping logs without a closed catalog.

    Args:
        code_root: Mainframe source tree the M5 analyze walk inspects.
            Must exist as a directory; matches the
            :func:`run_recon` invariant.
        mainframe_flavor: One of the
            :data:`primitives._shared.discovery.MainframeFlavor`
            Literals. Defaults to ``"cics_cobol"`` — the v1 fully-wired
            flavor. Passed through to both the recon and the analyze
            calls so they stay self-consistent.
        analyze_impl: Optional dependency-injection seam. When
            ``None`` (the production path), the function imports
            :func:`primitives._shared.discovery.run_recon` and
            :func:`primitives._shared.discovery.build_program_inventory`
            from the M5 package surface and chains them. When supplied,
            the callable is invoked with ``(code_root,
            mainframe_flavor)`` and is expected to return the
            ``(programs, copybooks, dag)`` tuple
            :func:`build_program_inventory` emits. Tests use this to
            assert the projection without spinning up the full M5
            walk; consumers can also use it to inject a richer analyzer.

    Returns:
        ``EntitiesDigest`` populated from the codebase walk. Sorted +
        deduplicated; ``schema_version`` is ``"1.0"``.

    Raises:
        FileNotFoundError: ``code_root`` does not exist (surfaced from
            :func:`run_recon`'s invariant — Phase A walks trees, not
            single files, so a missing root is a configuration error).
        NotADirectoryError: ``code_root`` exists but is not a directory.
        NotImplementedError: ``mainframe_flavor`` is a non-v1 value
            (``jcl_batch`` / ``ims_db``) — see issue #447 for the
            sibling-flavor wiring.

    Example:
        >>> from pathlib import Path
        >>> digest = ingest_from_codebase(Path("./mainframe-src"))
        >>> isinstance(digest.programs, list)
        True
    """
    impl = analyze_impl if analyze_impl is not None else _default_analyze_impl
    programs, copybooks, _dag = impl(code_root, mainframe_flavor)
    return _project_inventory_to_entities(programs, copybooks)


def _default_analyze_impl(
    code_root: Path,
    mainframe_flavor: MainframeFlavor,
) -> tuple[list[ProgramRecord], list[CopybookRecord], CopybookDag]:
    """Production dependency for :func:`ingest_from_codebase`.

    Chains :func:`primitives._shared.discovery.run_recon` (Phase A)
    into :func:`primitives._shared.discovery.build_program_inventory`
    (Phase B step 1) using the M5 public package surface — no
    cross-primitive ``shared/`` import (the regression guard in
    ``tests/test_no_cross_primitive_shared_imports.py`` enforces this).
    Kept as a module-level callable rather than a closure so the test
    suite can monkeypatch it and so the name shows up in tracebacks
    when the M5 walk raises.
    """
    recon_report = run_recon(code_root, mainframe_flavor=mainframe_flavor)
    return build_program_inventory(
        code_root,
        recon_report,
        flavor=mainframe_flavor,
    )


def _project_inventory_to_entities(
    programs: list[ProgramRecord],
    copybooks: list[CopybookRecord],
) -> EntitiesDigest:
    """Project the M5 inventory shape into the M6 ``EntitiesDigest``.

    Pulled out so the codebase path and any future re-use (e.g. a
    hybrid playbook + codebase merge) share a single projection
    routine. The set semantics + sorted output match
    :func:`ingest_from_playbook` so byte-stable cross-validation
    holds for any input that exercises both paths.
    """
    program_names: set[str] = set()
    for prog in programs:
        program_names.add(prog.name)
        # Unresolved call targets — M5's resolver leaves them on
        # `calls_to` with the raw symbol so Phase D can flag them. We
        # widen the M6 universe with them for the same reason the
        # playbook path widens with §5 Call Chain Map references: the
        # cascading-incident composer needs both ends of every edge.
        for target in prog.calls_to:
            program_names.add(target)

    copybook_names: set[str] = set()
    for cpy in copybooks:
        copybook_names.add(cpy.name)
        # Unresolved program → copybook COPY targets surface on
        # `prog.copybook_deps`; capture those too so the closed
        # universe matches whatever the source tree actually references.
    for prog in programs:
        for dep in prog.copybook_deps:
            copybook_names.add(dep)

    return EntitiesDigest(
        programs=sorted(program_names),
        files=[],
        copybooks=sorted(copybook_names),
        transactions=[],
        networks_and_ports=[],
    )


def write_entities_json(digest: EntitiesDigest, output_path: Path) -> None:
    """Persist an ``EntitiesDigest`` to ``output_path`` as JSON.

    The serialisation uses Pydantic's ``model_dump_json`` with
    ``indent=2`` for human-diffability — Phase A's output is small enough
    that pretty-printing adds negligible disk overhead and makes
    consumer-committed digests readable in PRs.

    The parent directory is created if missing. The file is written
    atomically (write-then-rename) so a partial write cannot leave
    downstream phases reading half a file.

    Args:
        digest: The ``EntitiesDigest`` Phase A produced.
        output_path: Destination JSON path. Convention is
            ``{--state-dir}/entities.json`` (design spec §"Output
            contract", Phase A column).

    Raises:
        OSError: write failed (permissions, disk full, etc).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    payload = digest.model_dump_json(indent=2) + "\n"
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(output_path)


def read_entities_json(input_path: Path) -> EntitiesDigest:
    """Load an ``EntitiesDigest`` from a JSON file Phase A previously
    persisted.

    Useful for downstream Phase B / C / D consumers that want to read
    Phase A's output without re-parsing the playbook. The schema-version
    guard on the model (``schema_version: Literal["1.0"]``) refuses a
    mismatched payload — a consumer who upgrades the library mid-pipeline
    sees the load fail, not a silent shape drift.

    Args:
        input_path: Path to the ``entities.json`` file Phase A wrote.

    Returns:
        ``EntitiesDigest`` parsed from disk.

    Raises:
        FileNotFoundError: ``input_path`` does not exist.
        pydantic.ValidationError: file content does not match the
            ``EntitiesDigest`` schema (typically a stale version).
    """
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    return EntitiesDigest.model_validate(payload)


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------


def _split_into_sections(text: str) -> dict[int, str]:
    """Slice the playbook into a ``{section_number: body_text}`` map.

    Returns only the sections enumerated in ``_REQUIRED_SECTIONS``.
    Each body spans from the line *after* the ``## N. Title`` header
    to the line *before* the next ``## `` header (or end of file).
    Missing sections are simply absent from the returned dict so the
    caller can fall back to ``sections.get(n, "")`` for empty-list
    parsing.
    """
    matches = list(_SECTION_RE.finditer(text))
    bodies: dict[int, str] = {}
    for idx, match in enumerate(matches):
        num = int(match.group("num"))
        if num not in _REQUIRED_SECTIONS:
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        bodies[num] = text[start:end]
    return bodies


# ---------------------------------------------------------------------------
# §3 Program Inventory
# ---------------------------------------------------------------------------


def _parse_programs(body: str) -> set[str]:
    """Extract program names from §3 Program Inventory.

    §3 ships four subsections of tables (Online CICS, ATM subsystem,
    Batch, PL/I Variants); each row's first column is a bold-wrapped
    program name. We don't care which subsection a program lives in —
    the closed universe is the union of all subsections.
    """
    if not body:
        return set()
    return {match.group(1) for match in _BOLD_TOKEN_RE.finditer(body)}


# ---------------------------------------------------------------------------
# §5 Call Chain Maps
# ---------------------------------------------------------------------------


def _parse_call_chain_programs(body: str) -> set[str]:
    """Extract every program name referenced in §5's call-chain
    evidence tables and Mermaid diagrams.

    The evidence tables list ``Source Program | Line | Target ...`` rows
    with bare uppercase identifiers (no bold wrapping); the Mermaid
    blocks use ``ID["NAME<br/>desc"]`` node declarations.

    A program referenced in §5 but absent from §3 (typically the
    ``SSTMT01P`` print-service stub or a copybook name accidentally
    picked up) is still folded into the universe — Phase B's
    cascading-failure composer walks call-chain edges, so missing one
    end of an edge breaks the day-3 incident narrative.
    """
    if not body:
        return set()
    # Mermaid node IDs: ``S00["SBANK00P<br/>Front Controller"]``
    mermaid_names = re.findall(r'\["([A-Z][A-Z0-9_]{4,15})(?:<br/>|"\])', body)
    # Plain table cells: ``| SBANK00P | 105 | STRAC00P | Static |``
    table_names = re.findall(r"\|\s*([A-Z][A-Z0-9]{4,15})\s*\|", body)
    # Targets in narrative text: programs separated by commas, "and", or "/"
    target_names = re.findall(r"\b([A-Z]{4}[0-9]{2}[A-Z])\b", body)
    return {*mermaid_names, *table_names, *target_names}


# ---------------------------------------------------------------------------
# §6 Data Architecture
# ---------------------------------------------------------------------------


def _parse_files(body: str) -> set[str]:
    """Extract VSAM file names + SQL table names from §6.

    VSAM files: §6.2's table lists ``**BNKCUST**``, ``**BNKACC**``,
    ``**BNKTXN**``, ``**BNKATYPE**``, ``**BNKHELP**`` as bold tokens.
    Also includes alternate-index names like ``BNKACC1`` and ``BNKTXN1``
    listed in the *Alternate Indexes* column.

    SQL tables: §6.6 lists ``BNKCUST``, ``BNKACC``, etc again in a
    table — they overlap with VSAM names by design (the SQL variant
    mirrors VSAM 1-to-1).
    """
    if not body:
        return set()
    # All bold tokens (catches the §6.2 + §6.6 inventory rows)
    bold = {match.group(1) for match in _BOLD_TOKEN_RE.finditer(body)}
    # Alternate-index names appear as ``BNKACC1 (AIX)`` or ``BNKTXN1``
    # in evidence blocks — capture them too.
    alt_indexes = set(re.findall(r"\b(BNK[A-Z]{2,15}\d)\b", body))
    # Dataset names like ``MFI01V.MFIDEMO.BNKCUST`` carry the file name
    # as the final dotted segment — extract those too.
    dataset_tails = set(re.findall(r"`[A-Z][A-Z0-9_.]+\.([A-Z][A-Z0-9]{2,15})`", body))
    return bold | alt_indexes | dataset_tails


def _parse_copybooks(body: str) -> set[str]:
    """Extract copybook names from §6.3 (Record Layouts) headings and
    from in-text path references.
    """
    if not body:
        return set()
    headers = {match.group("name") for match in _COPYBOOK_HEADER_RE.finditer(body)}
    paths = {match.group("name") for match in _COPYBOOK_PATH_RE.finditer(body)}
    return headers | paths


# ---------------------------------------------------------------------------
# §7 Screen Navigation & UI Layer
# ---------------------------------------------------------------------------


def _parse_transactions(body: str) -> set[str]:
    """Extract CICS transaction IDs + BMS map/mapset names from §7.

    The transaction ID itself (``BANK`` in the BankDemo reference)
    appears in §7.1's screen-flow diagram annotation
    ``(Transaction BANK)``. The BMS map inventory in §7.4 enumerates
    mapsets like ``MBANK10`` and individual maps like ``BANK10A``,
    ``HELP10A`` — these are bolded in the leftmost column.

    From M6's perspective every one of these is a *transaction-shaped*
    identifier the emitted CICS-transaction / CICS-access logs reference.
    """
    if not body:
        return set()
    transactions = {match.group(1) for match in _TRANSACTION_FROM_TEXT_RE.finditer(body)}
    # Mapset + map names from §7.4 ``| MBANK10 | BANK10A | ...``-style table.
    # First two columns of each row are the mapset and map; both should land.
    for row in body.splitlines():
        if "|" not in row:
            continue
        cells = [c.strip() for c in row.split("|") if c.strip()]
        for cell in cells[:2]:
            if re.fullmatch(r"(?:M)?BANK\d{2}[A-Z]?", cell) or re.fullmatch(
                r"HELP\d{2}[A-Z]?", cell
            ):
                transactions.add(cell)
    return transactions


# ---------------------------------------------------------------------------
# §9 Batch Processing
# ---------------------------------------------------------------------------


def _parse_jcl_jobs(body: str) -> set[str]:
    """Extract JCL job names from §9 (Batch Processing).

    §9.4 lists each ``ZBNKEXT1.jcl``, ``ZBNKPRT1.jcl`` etc as a
    backticked filename in the first column. The job name is the stem
    (everything before ``.jcl``). We also catch references in §9.1's
    pipeline diagram (``ZBNKEXT1``, ``ZBNKPRT1``, ``DBANK51P``).
    """
    if not body:
        return set()
    jcl_filenames = set(re.findall(r"`([A-Z][A-Z0-9]{2,15})\.jcl`", body))
    # Bold tokens too (the §9.3 batch-program table uses ``**ZBNKEXT1**``-style).
    bold = {match.group(1) for match in _BOLD_TOKEN_RE.finditer(body)}
    return jcl_filenames | bold


# ---------------------------------------------------------------------------
# §11 Deployment Architecture
# ---------------------------------------------------------------------------


def _parse_deployment(body: str) -> set[str]:
    """Extract hostnames, IP addresses, port numbers, and region names
    from §11 Deployment Architecture.

    Phase B's network-incident events (``network_drop``) need a closed
    set of IPs/ports to compose realistic socket-failure narratives;
    Phase D's IP-port-consistency rule rejects any log entry that
    mentions a host/port not in this list.
    """
    if not body:
        return set()
    out: set[str] = set()
    out.update(_IP_RE.findall(body))
    out.update(match.group(1) for match in _HOSTNAME_RE.finditer(body))
    out.update(match.group(1) for match in _PORT_RE.finditer(body))
    out.update(match.group(1) for match in _REGION_NAME_RE.finditer(body))
    return out


def run_phase_a(
    *,
    playbook_path: Path | None,
    code_root: Path | None,
) -> EntitiesDigest:
    """Dispatch Phase A ingest and return the ``EntitiesDigest``.

    This is the entry-point the CLI calls. It prefers the playbook path
    when ``playbook_path`` is supplied (Path 1 — :func:`ingest_from_playbook`);
    falls back to the codebase walk (Path 2 — :func:`ingest_from_codebase`)
    when only ``code_root`` is given. The CLI guarantees at least one of the
    two is a valid, resolved path before calling this function.

    Args:
        playbook_path: Path to an M5-produced ``PLAYBOOK.md``. When not
            ``None``, this path takes priority and the codebase walk is
            skipped. The CLI guarantees the file exists at this point
            (exit-code-2 guard fires earlier).
        code_root: Root of the mainframe source tree. Used only when
            ``playbook_path`` is ``None``. The CLI guarantees the directory
            exists at this point.

    Returns:
        ``EntitiesDigest`` populated from the preferred available source.

    Raises:
        FileNotFoundError: Propagated from the underlying ingest call when
            the path is stale between the CLI guard and Phase A execution
            (race condition; extremely unlikely in practice).
        OSError: Read failed (permissions, encoding, etc).
    """
    if playbook_path is not None:
        return ingest_from_playbook(playbook_path)
    # code_root is guaranteed non-None by the CLI guard at this point.
    assert code_root is not None, (
        "run_phase_a: both playbook_path and code_root are None — "
        "the CLI exit-code-2 guard should have caught this"
    )
    return ingest_from_codebase(code_root)


__all__ = [
    "ingest_from_codebase",
    "ingest_from_playbook",
    "read_entities_json",
    "run_phase_a",
    "write_entities_json",
]

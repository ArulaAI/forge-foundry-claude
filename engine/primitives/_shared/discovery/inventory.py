"""Discovery engine — Phase B step 1 ``build_program_inventory``.

Extracted from M5 ``playbook_discovery`` into the neutral
``primitives._shared.discovery`` package so the retained M6
``synth_logs`` primitive can consume the inventory engine without
depending on the (to-be-archived) M5 skill-first pipeline.

Walks ``--code-root`` (filtered by the ``--mainframe-flavor``-appropriate
:data:`SourceLanguage` buckets), drives the per-file flavor parser from
``primitives._shared.discovery.flavor`` over each file, and accumulates:

* A :class:`ProgramRecord` per program source file (COBOL ``.cbl`` /
  ``.cob`` for the v1 ``cics_cobol`` flavor). Program names come from the
  COBOL ``PROGRAM-ID`` paragraph; if the file lacks one, the filename
  stem is used as a fallback (with a logged warning).
* A :class:`CopybookRecord` per copybook source file (``.cpy`` /
  ``.copy`` for v1). Copybook names come from the filename stem
  (uppercased, ``-`` preserved) per the IBM convention.
* A :class:`CopybookDag` describing the copybook-to-copybook dependency
  graph that ``COPY`` statements inside copybooks induce. Cycles are
  detected via an iterative Tarjan SCC so a pathologically deep
  dependency chain cannot blow the Python recursion limit.

Design spec §M5 Phase B
(``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``)
+ issue [#448](https://github.com/arula-ai/forge-foundry-primitives/issues/448).
"""

from __future__ import annotations

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, Literal, cast

from primitives._shared.discovery.flavor import FLAVORS
from primitives._shared.discovery.models import (
    CopybookRecord,
    MainframeFlavor,
    ProgramRecord,
    SourceLanguage,
)
from primitives._shared.scan_exclusions import DEFAULT_EXCLUDED_DIRS

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from primitives._shared.discovery.flavor.cics_cobol import (
        CopybookIncludeRecord,
        ProgramCallRecord,
    )
    from primitives._shared.discovery.models import ReconReport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extension classification — mirrors shared/recon.py but kept local to
# avoid cross-module coupling. Same extension lists as the recon walker
# so Phase A and Phase B agree on what is a COBOL program vs a copybook.
# ---------------------------------------------------------------------------

_PROGRAM_EXTENSIONS: Final[frozenset[str]] = frozenset({".cbl", ".cob"})
_COPYBOOK_EXTENSIONS: Final[frozenset[str]] = frozenset({".cpy", ".copy"})

# COBOL column-7 indicator area — used by the PROGRAM-ID extractor to
# skip comment lines. Mirrors shared/flavor/cics_cobol._INDICATOR_COLUMN
# but kept local to avoid coupling on a private constant.
_COBOL_INDICATOR_COLUMN: Final[int] = 7
_COBOL_INDICATOR_OFFSET: Final[int] = _COBOL_INDICATOR_COLUMN - 1
_COBOL_COMMENT_MARKERS: Final[frozenset[str]] = frozenset({"*", "/"})

# Mapping from flavor → (program extensions, copybook extensions). v1
# only wires ``cics_cobol``; sibling flavors land in M5-FLAVOR-2 with
# their own extension sets (e.g. ``.pli`` / ``.inc`` for PL/I, ``.jcl``
# for JCL — note JCL "jobs" are not COBOL programs and feed
# JCLJobRecord, which M5-ANALYZE-2 handles, not this step).
_FLAVOR_LANGUAGE_MAP: Final[dict[MainframeFlavor, tuple[frozenset[str], frozenset[str]]]] = {
    "cics_cobol": (_PROGRAM_EXTENSIONS, _COPYBOOK_EXTENSIONS),
}


# ---------------------------------------------------------------------------
# Regex — extract COBOL PROGRAM-ID
# ---------------------------------------------------------------------------

#: COBOL ``PROGRAM-ID. <name>.`` paragraph. We accept optional ``IS
#: COMMON`` / ``IS INITIAL`` / ``IS RECURSIVE`` suffixes (89 / 2002
#: standard) but only capture the bare program name. The name itself
#: follows COBOL identifier rules: letter first, letters / digits /
#: hyphens after.
_PROGRAM_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"\bPROGRAM-ID\s*\.\s*(?P<name>[A-Za-z][A-Za-z0-9\-]*)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# CopybookDag — edge / cycle output for the copybook dependency graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopybookDagEdge:
    """One directed edge in the copybook → copybook dependency graph.

    ``source`` is the copybook that contains the ``COPY`` statement.
    ``target`` is the copybook the COPY references. ``resolved`` is
    ``True`` when ``target`` appears in the inventory's
    :class:`CopybookRecord` list — unresolved targets stay on the DAG
    so Phase D can surface the broken reference rather than the
    aggregator silently dropping it.

    ``line`` is the 1-based source line number of the COPY statement
    (consulted by the evidence-ledger writer in M5-LEDGER-1).
    """

    source: str
    target: str
    resolved: bool
    line: int


@dataclass(frozen=True)
class ProgramCallEdge:
    """One directed edge in the program → program call graph.

    ``source`` is the calling program. ``target`` is the called program
    (or a variable name when ``resolved=False``). ``call_kind`` mirrors
    :class:`ProgramCallRecord.call_kind` so §5 Call Chain Maps can
    render the edge label. ``line`` is the 1-based source line number
    of the CALL / LINK / XCTL statement.
    """

    source: str
    target: str
    call_kind: Literal["link", "xctl", "call_static", "call_dynamic"]
    resolved: bool
    line: int


@dataclass
class CopybookDag:
    """Copybook dependency graph + program-call graph metadata.

    Returned as the third element of
    :func:`build_program_inventory`'s return tuple. The name preserves
    the "copybook DAG" framing from the design spec — the
    :attr:`program_call_edges` field carries the parallel program-call
    edge resolution data that Phase C / D consumers iterate alongside
    the copybook edges.

    ``nodes`` is the sorted list of copybook names known to the
    inventory (the universe the DAG operates over). ``edges`` is the
    list of resolved + unresolved copybook → copybook references.
    ``cycles`` is the list of strongly-connected components of size > 1
    (plus any self-loops) — copybook chains *should* be a DAG (no
    cycle), but pathological hand-edited copybooks can self-include and
    the auditor needs to surface that.
    """

    nodes: list[str] = field(default_factory=list)
    edges: list[CopybookDagEdge] = field(default_factory=list)
    cycles: list[list[str]] = field(default_factory=list)
    program_call_edges: list[ProgramCallEdge] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_program_inventory(
    code_root: Path,
    recon_report: ReconReport,
    *,
    flavor: MainframeFlavor = "cics_cobol",
) -> tuple[list[ProgramRecord], list[CopybookRecord], CopybookDag]:
    """Build the program inventory + copybook DAG from ``code_root``.

    Walks ``code_root`` (filtered by ``flavor``-appropriate source
    extensions), drives :func:`parse_cobol_source` over each file, and
    accumulates the canonical Phase B step 1 sub-payload of
    :class:`AnalysisDigest`.

    Args:
        code_root: Directory containing the mainframe source tree
            (typically the same value the Phase A walker received). Must
            exist as a directory; a missing root raises
            :class:`FileNotFoundError`, matching the
            :func:`run_recon` invariant.
        recon_report: The Phase A :class:`ReconReport`. Currently
            consulted only for the ``mainframe_flavor`` cross-check —
            if ``recon_report.mainframe_flavor`` differs from the
            caller's ``flavor`` argument, a warning is logged but the
            ``flavor`` argument wins (the explicit caller intent
            overrides the recon-time setting so re-runs with a
            different flavor stay self-consistent).
        flavor: One of the :data:`MainframeFlavor` Literals. Only
            ``"cics_cobol"`` is wired in v1; the others raise
            :class:`NotImplementedError` with a future-work pointer
            (matches the design spec's M5-FLAVOR-2 carve-out).

    Returns:
        ``(programs, copybooks, dag)`` where ``programs`` is the list of
        :class:`ProgramRecord` instances (one per COBOL source file),
        ``copybooks`` is the list of :class:`CopybookRecord` instances
        (one per copybook source file), and ``dag`` is the
        :class:`CopybookDag` describing the copybook-to-copybook
        dependency graph + the parallel program-call edge resolution
        data.

    Raises:
        FileNotFoundError: If ``code_root`` does not exist.
        NotADirectoryError: If ``code_root`` exists but is not a
            directory.
        NotImplementedError: If ``flavor`` is a non-v1 value
            (``jcl_batch`` / ``ims_db``) — see issue #447 for the
            sibling-flavor wiring.
    """
    if not code_root.exists():
        raise FileNotFoundError(f"--code-root not found: {code_root}")
    if not code_root.is_dir():
        raise NotADirectoryError(f"--code-root is not a directory: {code_root}")

    if flavor not in _FLAVOR_LANGUAGE_MAP:
        raise NotImplementedError(
            f"M5 Phase B step 1: flavor {flavor!r} not wired in v1 — "
            f"sibling flavors land in M5-FLAVOR-2 (#447). v1 supports "
            f"only 'cics_cobol'."
        )
    if flavor not in FLAVORS:  # pragma: no cover — registry desync guard
        # Defensive: a flavor registered in _FLAVOR_LANGUAGE_MAP but
        # missing from FLAVORS would be a registry desync. The
        # NotImplementedError points at the registry surface so the
        # contributor knows where to wire the parser.
        raise NotImplementedError(
            f"M5 Phase B step 1: flavor {flavor!r} has language buckets "
            f"but no registered parser in shared/flavor/__init__.FLAVORS. "
            f"Add the parser entry there (M5-FLAVOR-2 / #447)."
        )

    if recon_report.mainframe_flavor is not None and recon_report.mainframe_flavor != flavor:
        logger.warning(
            "analyze: recon report flavor %r differs from analyse flavor %r — using analyse flavor",
            recon_report.mainframe_flavor,
            flavor,
        )

    state = _collect_files(code_root, flavor)

    program_call_edges, calls_to, called_by = _resolve_program_calls(state)
    copybook_deps, used_by = _resolve_program_copybook_deps(state)
    dag_edges, dag_adjacency = _resolve_copybook_dag_edges(state)

    cycles_keys = _find_cycles(dag_adjacency)
    cycles = [[state.copybooks[state.copybook_by_name[k]].name for k in scc] for scc in cycles_keys]

    dag = CopybookDag(
        nodes=sorted(state.copybooks[idx].name for idx in state.copybook_by_name.values()),
        edges=dag_edges,
        cycles=cycles,
        program_call_edges=program_call_edges,
    )

    programs = [
        prog.model_copy(
            update={
                "calls_to": calls_to[prog.name.upper()],
                "called_by": called_by[prog.name.upper()],
                "copybook_deps": copybook_deps[prog.name.upper()],
            }
        )
        for prog in state.programs
    ]
    copybooks = [
        cpy.model_copy(update={"used_by_programs": used_by[cpy.name.upper()]})
        for cpy in state.copybooks
    ]

    return programs, copybooks, dag


# ---------------------------------------------------------------------------
# Per-file collection helpers (broken out of build_program_inventory)
# ---------------------------------------------------------------------------


@dataclass
class _InventoryState:
    """Per-file collection scratch-state used by edge resolution.

    Lives only inside :func:`build_program_inventory`; the public
    surface is the ``(programs, copybooks, dag)`` tuple. Keys are
    upper-cased entity names so edge resolution can be case-insensitive
    (COBOL identifiers are case-insensitive by convention).
    """

    programs: list[ProgramRecord] = field(default_factory=list)
    copybooks: list[CopybookRecord] = field(default_factory=list)
    program_by_name: dict[str, int] = field(default_factory=dict)
    copybook_by_name: dict[str, int] = field(default_factory=dict)
    program_outgoing_calls: dict[str, list[ProgramCallRecord]] = field(
        default_factory=dict,
    )
    program_copybook_includes: dict[str, list[CopybookIncludeRecord]] = field(
        default_factory=dict,
    )
    copybook_outgoing_copies: dict[str, list[CopybookIncludeRecord]] = field(
        default_factory=dict,
    )


def _collect_files(code_root: Path, flavor: MainframeFlavor) -> _InventoryState:
    """Walk ``code_root`` and assemble the per-file collection state.

    One file → one record (program or copybook) plus per-file extracted
    edge records the resolution helpers consume. Mirrors the recon
    walker's degrade-on-OSError invariant.
    """
    program_exts, copybook_exts = _FLAVOR_LANGUAGE_MAP[flavor]
    parser = FLAVORS[flavor]
    state = _InventoryState()

    for source_file in _iter_sources(code_root, program_exts | copybook_exts):
        try:
            content = source_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("analyze: could not read %s: %s", source_file, exc)
            continue

        ext = source_file.suffix.lower()
        rel_path = _relative_path(source_file, code_root)
        line_count = _count_lines(content)
        parsed = parser(source_file, content)

        if ext in program_exts:
            _add_program(state, source_file, content, rel_path, line_count, parsed)
        elif ext in copybook_exts:
            _add_copybook(state, source_file, content, rel_path, parsed)

    return state


def _add_program(
    state: _InventoryState,
    source_file: Path,
    content: str,
    rel_path: str,
    line_count: int,
    parsed: dict[str, list[object]],
) -> None:
    """Append a :class:`ProgramRecord` to ``state`` (or skip on duplicate)."""
    name = _extract_program_id(content) or _filename_stem(source_file)
    language: SourceLanguage = "cobol"
    program_record = ProgramRecord(
        name=name,
        language=language,
        source_path=rel_path,
        line_count=line_count,
    )
    key = name.upper()
    if key in state.program_by_name:
        logger.warning(
            "analyze: duplicate program name %r at %s (already at %s) — skipping duplicate",
            name,
            rel_path,
            state.programs[state.program_by_name[key]].source_path,
        )
        return
    state.program_by_name[key] = len(state.programs)
    state.programs.append(program_record)
    state.program_outgoing_calls[key] = cast(
        "list[ProgramCallRecord]", list(parsed["program_calls"])
    )
    state.program_copybook_includes[key] = cast(
        "list[CopybookIncludeRecord]", list(parsed["copybook_includes"])
    )


def _add_copybook(
    state: _InventoryState,
    source_file: Path,
    content: str,
    rel_path: str,
    parsed: dict[str, list[object]],
) -> None:
    """Append a :class:`CopybookRecord` to ``state`` (or skip on duplicate)."""
    name = _filename_stem(source_file)
    copybook_record = CopybookRecord(name=name, record_layout=content)
    key = name.upper()
    if key in state.copybook_by_name:
        logger.warning(
            "analyze: duplicate copybook name %r at %s — skipping",
            name,
            rel_path,
        )
        return
    state.copybook_by_name[key] = len(state.copybooks)
    state.copybooks.append(copybook_record)
    state.copybook_outgoing_copies[key] = cast(
        "list[CopybookIncludeRecord]", list(parsed["copybook_includes"])
    )


def _resolve_program_calls(
    state: _InventoryState,
) -> tuple[list[ProgramCallEdge], dict[str, list[str]], dict[str, list[str]]]:
    """Resolve CALL/LINK/XCTL edges into ``program_call_edges`` + accumulators.

    Returns ``(edges, calls_to, called_by)`` where ``calls_to`` and
    ``called_by`` are keyed on upper-cased program names and carry the
    deduplicated edge target / source names in sorted order. The
    edge list ``program_call_edges`` is the full per-statement edge log
    (including duplicates within a single program) so Phase D and §5
    Call Chain Maps can render the underlying source statement count.

    Internally accumulates into ``dict[str, set[str]]`` for O(1) membership
    tests, then emits sorted lists so the output shape is identical to the
    previous list-based dedup but without the O(n²) ``if x not in list`` cost.
    """
    edges: list[ProgramCallEdge] = []
    called_by_sets: dict[str, set[str]] = {key: set() for key in state.program_by_name}
    calls_to_sets: dict[str, set[str]] = {key: set() for key in state.program_by_name}

    for caller_key, call_records in state.program_outgoing_calls.items():
        caller_name = state.programs[state.program_by_name[caller_key]].name
        for call in call_records:
            target_key = call.target.upper() if call.resolved else None
            resolved_in_inventory = (
                call.resolved and target_key is not None and target_key in state.program_by_name
            )
            if resolved_in_inventory and target_key is not None:
                target_name = state.programs[state.program_by_name[target_key]].name
            else:
                target_name = call.target
            edges.append(
                ProgramCallEdge(
                    source=caller_name,
                    target=target_name,
                    call_kind=call.call_kind,
                    resolved=resolved_in_inventory,
                    line=call.line,
                )
            )
            calls_to_sets[caller_key].add(target_name)
            if resolved_in_inventory and target_key is not None:
                called_by_sets[target_key].add(caller_name)

    calls_to: dict[str, list[str]] = {k: sorted(v) for k, v in calls_to_sets.items()}
    called_by: dict[str, list[str]] = {k: sorted(v) for k, v in called_by_sets.items()}
    return edges, calls_to, called_by


def _resolve_program_copybook_deps(
    state: _InventoryState,
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Resolve program → copybook ``COPY`` edges.

    Returns ``(copybook_deps, used_by_programs)`` accumulators keyed on
    upper-cased program / copybook names. An unresolved COPY (the
    target copybook is not in the inventory) still appears on
    ``copybook_deps`` so Phase D can flag the broken reference; the
    target's back-pointer is NOT invented.

    Internally accumulates into ``dict[str, set[str]]`` for O(1) membership
    tests, then emits sorted lists so the output shape is identical to the
    previous list-based dedup but without the O(n²) ``if x not in list`` cost.
    """
    copybook_deps_sets: dict[str, set[str]] = {key: set() for key in state.program_by_name}
    used_by_sets: dict[str, set[str]] = {key: set() for key in state.copybook_by_name}

    for caller_key, include_records in state.program_copybook_includes.items():
        caller_name = state.programs[state.program_by_name[caller_key]].name
        for inc in include_records:
            target_key = inc.name.upper()
            if target_key in state.copybook_by_name:
                target_name = state.copybooks[state.copybook_by_name[target_key]].name
            else:
                target_name = inc.name
            copybook_deps_sets[caller_key].add(target_name)
            if target_key in state.copybook_by_name:
                used_by_sets[target_key].add(caller_name)

    copybook_deps: dict[str, list[str]] = {k: sorted(v) for k, v in copybook_deps_sets.items()}
    used_by: dict[str, list[str]] = {k: sorted(v) for k, v in used_by_sets.items()}
    return copybook_deps, used_by


def _resolve_copybook_dag_edges(
    state: _InventoryState,
) -> tuple[list[CopybookDagEdge], dict[str, set[str]]]:
    """Resolve copybook → copybook ``COPY`` edges into DAG edges + adjacency.

    Returns ``(edges, adjacency)`` where ``edges`` is the flat edge log
    (including unresolved targets) and ``adjacency`` is the
    upper-cased-key map :func:`_find_cycles` consumes. Unresolved
    targets are NOT added to the adjacency map (a phantom edge cannot
    participate in a cycle in the inventory's name-space).
    """
    edges: list[CopybookDagEdge] = []
    adjacency: dict[str, set[str]] = {key: set() for key in state.copybook_by_name}

    for source_key, include_records in state.copybook_outgoing_copies.items():
        source_name = state.copybooks[state.copybook_by_name[source_key]].name
        for inc in include_records:
            target_key = inc.name.upper()
            target_in_inventory = target_key in state.copybook_by_name
            if target_in_inventory:
                target_name = state.copybooks[state.copybook_by_name[target_key]].name
            else:
                target_name = inc.name
            edges.append(
                CopybookDagEdge(
                    source=source_name,
                    target=target_name,
                    resolved=target_in_inventory,
                    line=inc.line,
                )
            )
            if target_in_inventory:
                adjacency[source_key].add(target_key)
    return edges, adjacency


# ---------------------------------------------------------------------------
# Walker helpers — mirrors shared/recon.py's _walk semantics
# ---------------------------------------------------------------------------


def _iter_sources(root: Path, extensions: frozenset[str]) -> Iterable[Path]:
    """Yield every file under ``root`` whose suffix is in ``extensions``.

    Deterministic order (sorted within each directory) so the inventory
    list ordering is byte-stable across runs — the M3 / M1 precedent.
    Symlinks are skipped (matches the recon walker invariant) so a
    crafted symlink cannot escape ``root``.
    """
    queue: deque[Path] = deque([root])
    while queue:
        current = queue.popleft()
        try:
            children = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError as exc:  # pragma: no cover — defended against
            logger.warning("analyze: could not list %s: %s", current, exc)
            continue
        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir():
                # Skip dot-dirs AND build-output / dependency dirs (#1359) so the
                # inventory does not ingest compiled/test artifacts from
                # ``target/`` / ``build/`` / ``node_modules/`` as source.
                if child.name.startswith(".") or child.name in DEFAULT_EXCLUDED_DIRS:
                    continue
                queue.append(child)
                continue
            if child.suffix.lower() in extensions:
                yield child


def _extract_program_id(content: str) -> str | None:
    """Extract the COBOL PROGRAM-ID from ``content``.

    Returns ``None`` if no ``PROGRAM-ID. <name>.`` paragraph is found
    (the caller falls back to the filename stem and logs the gap so
    Phase D can surface a malformed source).

    The walk strips column-7 comment lines BEFORE matching so a
    commented-out PROGRAM-ID doesn't shadow a real one. The match is
    case-insensitive on the keyword (modern dialects accept lower-case);
    the captured identifier is returned verbatim so the output preserves
    the source's casing.
    """
    # Strip comment-indicator lines (column 7 = '*' or '/'). The
    # heuristic matches shared/flavor/cics_cobol._is_comment_line; we
    # don't import it to avoid coupling the public API on a private
    # helper.
    lines: list[str] = []
    for raw in content.splitlines():
        if (
            len(raw) >= _COBOL_INDICATOR_COLUMN
            and raw[_COBOL_INDICATOR_OFFSET] in _COBOL_COMMENT_MARKERS
        ):
            continue
        lines.append(raw)
    match = _PROGRAM_ID_RE.search("\n".join(lines))
    if match is None:
        return None
    return match.group("name")


def _filename_stem(path: Path) -> str:
    """Return ``path``'s filename without the extension.

    Used as the program name fallback (when PROGRAM-ID is missing) and
    as the canonical copybook name. Case is preserved verbatim from the
    filesystem so a copybook saved as ``Account.cpy`` does not silently
    become ``ACCOUNT`` — the upper-cased *key* is used for lookups but
    the *name* round-trips the filesystem casing.
    """
    return path.stem


def _relative_path(path: Path, code_root: Path) -> str:
    """Return ``path`` rendered relative to ``code_root`` (POSIX style).

    Phase A and Phase B both record repo-relative paths so the
    evidence-ledger ``path`` field is stable run-over-run regardless of
    the absolute path the user invoked the CLI from.
    """
    try:
        return path.relative_to(code_root).as_posix()
    except ValueError:  # pragma: no cover — defended against
        return path.as_posix()


def _count_lines(content: str) -> int:
    """Return the line count of ``content`` (matches recon._count_lines).

    Kept local so analyze.py does not import a private helper from
    recon.py (the modules are independent — Phase B can run with a
    hand-rolled ReconReport injected by a test).
    """
    if not content:
        return 0
    return len(content.splitlines())


# ---------------------------------------------------------------------------
# Cycle detection — iterative Tarjan SCC (mirrors M3 sequencer pattern)
# ---------------------------------------------------------------------------


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return the list of cycle-participating SCCs in ``graph``.

    Tarjan's algorithm, iterative form (mirrors the M3 sequencer at
    PR #354) so a pathologically deep copybook chain cannot blow the
    Python recursion limit. Output is deterministic: nodes are visited
    in sorted order, and the SCCs within each cycle are sorted, so the
    returned list is byte-stable run-over-run.

    Only SCCs of size > 1 OR single-node SCCs with a self-loop are
    returned — a non-self-looping singleton is not a cycle. The
    self-loop guard mirrors M3's ``_has_self_loop`` (M3's Python
    pipeline was archived in the charter-v2 reshape — recover via the
    ``v1.1.0-pre-archival`` tag).
    """
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(start: str) -> None:
        # Iterative Tarjan: explicit work-stack of (node, successors).
        work: list[tuple[str, deque[str]]] = [(start, deque(sorted(graph.get(start, set()))))]
        indices[start] = index_counter[0]
        lowlinks[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, successors = work[-1]
            advanced = False
            while successors:
                succ = successors.popleft()
                if succ not in indices:
                    indices[succ] = index_counter[0]
                    lowlinks[succ] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(succ)
                    on_stack.add(succ)
                    work.append((succ, deque(sorted(graph.get(succ, set())))))
                    advanced = True
                    break
                if succ in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[succ])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlinks[parent] = min(lowlinks[parent], lowlinks[node])
            if lowlinks[node] == indices[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                sccs.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            strongconnect(node)

    # Filter to cycle-participating SCCs only (size > 1 OR self-loop).
    cycles: list[list[str]] = []
    for scc in sccs:
        if len(scc) > 1 or (len(scc) == 1 and scc[0] in graph.get(scc[0], set())):
            cycles.append(scc)
    return cycles


__all__ = [
    "CopybookDag",
    "CopybookDagEdge",
    "ProgramCallEdge",
    "build_program_inventory",
]

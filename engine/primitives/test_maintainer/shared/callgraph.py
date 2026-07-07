"""Downstream call-graph traversal and gap detection (S2 TM-3, #895).

This module augments the coverage-primary impact engine (TM-2,
``impact.py``) with two complementary capabilities:

1. **Downstream caller traversal.** For each changed symbol, follow
   transitive *callers* via :meth:`TestStackAdapter.call_graph`, then find
   tests that exercise those caller symbols via the static dep-graph walk,
   and union them into the impact set as ``source="static-callgraph"``
   entries.

2. **Gap detection.** A changed or new symbol with *neither* coverage-map
   coverage *nor* any test reachable via the call graph is an
   ``uncovered-changed-symbol`` — a gap candidate.  It is surfaced as an
   :class:`~primitives.test_maintainer.shared.impact.ImpactEntry` carrying
   ``gap=True`` so the proposer (phase 3) can emit ``add`` proposals for it.

**Graceful degradation.** When
:meth:`~primitives._sdet.stacks.contract.TestStackAdapter.call_graph`
returns ``None`` (JS/TS and Java adapters in TM-8/TM-9; Python on
unexpected failure; or no adapter at all), both augments are silently
skipped and the engine relies entirely on coverage + import-reachability.

**Upstream edges (callees).** Callee-direction edges are captured as a
side effect of the full graph walk but are *not* used in the engine today.
The :class:`~primitives._sdet.stacks.contract.CallGraph` already stores
them (they are symmetric in ``edges``) and can be queried by future phases.

**Symbol qualification.** The Python adapter uses
``<repo-relative-posix>:<func>`` or ``<repo-relative-posix>:<Class.method>``.
When a changed symbol from the TM-2 diff is a bare name (e.g. ``"foo"``),
:func:`augment_with_callgraph` matches it against any qualified edge whose
suffix is ``:foo`` so callers can pass the bare diff-symbol names directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._sdet.stacks.contract import CallGraph, SymbolSet
    from primitives.test_maintainer.shared.impact import ImpactEntry

_logger = logging.getLogger(__name__)

#: Maximum BFS depth for transitive caller traversal.  Prevents runaway
#: expansion in highly-connected graphs.
_MAX_TRAVERSAL_DEPTH = 5


def _bare_name(symbol: str) -> str:
    """Return the bare function / method name from a qualified symbol.

    ``"src/calc/ops.py:add"`` → ``"add"``
    ``"src/calc/ops.py:Calc.add"`` → ``"add"``
    ``"add"`` → ``"add"``
    """
    # strip leading ``<path>:`` qualifier
    after_colon = symbol.rsplit(":", 1)[-1]
    # strip leading ``<ClassName>.`` qualifier
    return after_colon.rsplit(".", 1)[-1]


def _symbol_matches(graph_symbol: str, target: str) -> bool:
    """True when *graph_symbol* resolves to *target*.

    Handles three matching modes:
    - exact string equality (fastest path)
    - graph_symbol ends with ``":<target>"`` (qualified → bare)
    - graph_symbol ends with ``".<target>"`` (qualified method → bare method)
    """
    if graph_symbol == target:
        return True
    return graph_symbol.endswith(f":{target}") or graph_symbol.endswith(f".{target}")


def _transitive_callers(
    cg: CallGraph,
    seed_symbols: frozenset[str],
    max_depth: int = _MAX_TRAVERSAL_DEPTH,
) -> frozenset[str]:
    """BFS over *cg* starting from *seed_symbols*, following caller edges.

    Returns the set of *all* transitive callers (excluding the seeds
    themselves).  Bounded by *max_depth* to prevent runaway traversal.

    Matching is by :func:`_symbol_matches` so bare seed names (from the
    TM-2 diff) are resolved to their qualified graph-symbol caller set.
    """
    visited: set[str] = set()
    frontier: set[str] = set(seed_symbols)

    for _depth in range(max_depth):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for sym in frontier:
            # Direct callers: all edges whose callee matches this symbol.
            callers = frozenset(e.caller for e in cg.edges if _symbol_matches(e.callee, sym))
            new_callers = callers - visited - seed_symbols
            visited.update(new_callers)
            next_frontier.update(new_callers)
        frontier = next_frontier

    return frozenset(visited)


def augment_with_callgraph(
    *,
    project_root: Path,
    changed_symbols: dict[str, Any],  # symbol -> ImpactClass
    existing_pairs: dict[tuple[str, str], str],  # (sym, test_id) -> source
    adapter: Any | None,
    symbols: SymbolSet,
) -> dict[tuple[str, str], str]:
    """Augment *existing_pairs* with call-graph-reachable (symbol, test) pairs.

    Returns a *copy* of *existing_pairs* with any new
    ``(symbol, test_id) → "static-callgraph"`` entries added for:
    - changed symbols whose transitive callers have tests in the static
      dep-graph, and
    - no new pairs are added when *adapter* is None or ``call_graph``
      returns None (graceful degradation).

    Args:
        project_root: Absolute project root.
        changed_symbols: Map of changed symbol names (bare or qualified) →
            :class:`~primitives.test_maintainer.shared.impact.ImpactClass`.
        existing_pairs: The ``(symbol, test_id) → source`` dict already
            built by the coverage-primary + static legs.
        adapter: A :class:`~primitives._sdet.stacks.contract.TestStackAdapter`
            (may be None — skips the augment entirely).
        symbols: The :class:`~primitives._sdet.stacks.contract.SymbolSet`
            passed to ``call_graph``.

    Returns:
        A merged dict (new dict; does not mutate *existing_pairs*).
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from primitives._sdet.depgraph import find_affected_tests  # noqa: PLC0415

    pairs = dict(existing_pairs)

    if adapter is None:
        return pairs

    # call_graph may be absent (old-style fakes / adapters not implementing TM-3)
    # or may return None (graceful degradation for JS/TS, Java, Python failure).
    cg_fn = getattr(adapter, "call_graph", None)
    if cg_fn is None:
        return pairs
    cg: CallGraph | None = cg_fn(_Path(str(project_root)), symbols)
    if cg is None:
        return pairs

    if not changed_symbols:
        return pairs

    seed = frozenset(changed_symbols.keys())
    callers = _transitive_callers(cg, seed)
    if not callers:
        return pairs

    # For each caller, derive the source file it likely lives in so we can
    # feed it to find_affected_tests.  The qualified form is
    # ``<rel-path>:<func>``; we extract the path part.
    caller_files: set[_Path] = set()
    for caller in callers:
        if ":" in caller:
            rel_part = caller.rsplit(":", 1)[0]
            full = _Path(str(project_root)) / rel_part
            if full.is_file():
                caller_files.add(full)

    if not caller_files:
        return pairs

    # find_affected_tests returns {source_file: set[test_Path]}
    static_map = find_affected_tests(sorted(caller_files), _Path(str(project_root)))
    for _cf, test_paths in static_map.items():
        for sym in changed_symbols:
            for tp in sorted(test_paths):
                try:
                    tid = _Path(str(tp)).resolve().relative_to(_Path(str(project_root))).as_posix()
                except ValueError:
                    tid = _Path(str(tp)).as_posix()
                key = (sym, tid)
                if key not in pairs:
                    pairs[key] = "static-callgraph"

    return pairs


def compute_gaps(
    *,
    changed_symbols: dict[str, Any],  # symbol -> ImpactClass
    covered_symbols: frozenset[str],
) -> frozenset[str]:
    """Return changed symbols with *no* test reachability — gap candidates.

    A symbol is a gap when it does not appear (by bare or exact name) in
    *covered_symbols* (the union of symbols that have at least one test
    in *existing_pairs*).

    Args:
        changed_symbols: All changed/added/removed symbols.
        covered_symbols: Symbols that already have at least one (symbol,
            test) pair in the impact set.

    Returns:
        Frozenset of gap symbol names (bare strings from *changed_symbols*).
    """
    gaps: set[str] = set()
    for sym in changed_symbols:
        bare = _bare_name(sym)
        # The symbol is covered if it (or its bare form) appears in
        # covered_symbols.
        if sym not in covered_symbols and bare not in covered_symbols:
            gaps.add(sym)
    return frozenset(gaps)


def build_gap_entries(
    *,
    gap_symbols: frozenset[str],
    changed_symbols: dict[str, Any],  # symbol -> ImpactClass
) -> list[ImpactEntry]:
    """Convert *gap_symbols* into :class:`ImpactEntry` gap-marker records.

    Each gap entry has ``test_id=""`` (no test found), ``gap=True`` in
    serialised form, and ``source="gap"`` so the proposer can emit ``add``
    proposals.  The ``ImpactEntry`` dataclass is extended to carry a
    ``gap`` flag via the ``source`` field value ``"gap"`` (a distinct
    source string, not a separate field — keeps the existing ``to_dict``
    contract intact; callers check ``entry.source == "gap"``).

    Returns a stable-sorted list.
    """
    from primitives.test_maintainer.shared.impact import ImpactEntry  # noqa: PLC0415

    entries: list[ImpactEntry] = []
    for sym in sorted(gap_symbols):
        ic = changed_symbols.get(sym)
        if ic is None:
            # Bare name lookup: find first match
            for k, v in changed_symbols.items():
                if _bare_name(k) == sym or k == sym:
                    ic = v
                    break
        if ic is None:
            continue
        entries.append(
            ImpactEntry(
                symbol=sym,
                test_id="",
                impact_class=ic,
                source="gap",
            )
        )
    return entries


__all__ = [
    "augment_with_callgraph",
    "build_gap_entries",
    "compute_gaps",
]

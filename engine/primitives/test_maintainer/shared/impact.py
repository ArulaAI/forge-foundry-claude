"""Impact classifier and coverage-primary impact engine (S2 TM-2, #894).

Phase 0 (seed): extract changed/added/removed symbols (functions, methods,
classes) at line granularity from the git diff.
Phase 1 (impact): coverage-primary hybrid — CoverageMap.impacted_by() is the
source of truth; the static dependency-graph walk (find_affected_tests) augments
when coverage is absent or a symbol has no covering test.

Public API (new in TM-2):
    build_changed_set   — parse git diff hunks → ChangedSet
    compute_impacted    — coverage-primary + static fallback → list[ImpactEntry]
    write_impact_artifacts — emit impacted.json + coverage-map.json

Preserved (TM-1 callers use these):
    ImpactClass, ImpactReport, classify_impact
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._sdet.stacks.contract import ChangedSet, CoverageMap

_logger = logging.getLogger(__name__)

_BEHAVIOR_DELTA_THRESHOLD = 0.30  # >30% line delta = behavior change


class ImpactClass(Enum):
    SIGNATURE = "signature"
    BEHAVIOR = "behavior"
    COVERAGE = "coverage"


@dataclass(frozen=True)
class ImpactReport:
    test_file: Path
    impacts: frozenset[ImpactClass]
    affected_functions: tuple[str, ...]


@dataclass(frozen=True)
class ImpactEntry:
    """One (symbol, test) pair in the impact set.

    Emitted to ``impacted.json`` for the proposer (phase 3) and for human
    audit. ``source`` distinguishes how the pair was found:

    - ``"coverage"`` — runtime coverage map (spec §4, primary source).
    - ``"static"`` — import dep-graph walk (static fallback/augment).
    - ``"static-callgraph"`` — downstream call-graph traversal (TM-3).
    - ``"declared-e2e"`` — a route/surface change matched a declared
      ``.foundry/e2e-map.yaml`` rule (#1029); ``test_id`` is the e2e spec path
      and ``symbol`` is the matched surface glob.
    - ``"gap"`` — changed symbol with no reachable test (TM-3 gap
      detection); ``test_id`` is the empty string for gap entries.
    """

    symbol: str
    test_id: str
    impact_class: ImpactClass
    source: str  # "coverage" | "static" | "static-callgraph" | "gap"

    @property
    def gap(self) -> bool:
        """True when this entry is an uncovered-symbol gap candidate."""
        return self.source == "gap"

    def to_dict(self) -> dict[str, str | bool]:
        d: dict[str, str | bool] = {
            "symbol": self.symbol,
            "test_id": self.test_id,
            "impact_class": self.impact_class.value,
            "source": self.source,
        }
        if self.gap:
            d["gap"] = True
        return d


# ---------------------------------------------------------------------------
# Existing helpers (unchanged — preserves all callers in TM-1)
# ---------------------------------------------------------------------------


def _collect_functions(source: str) -> dict[str, ast.FunctionDef]:
    """Return map of name → FunctionDef."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            out[node.name] = node
    return out


def _names_referenced_in_test(test_content: str) -> frozenset[str]:
    """Return every identifier the test file references (Name, Attribute, FunctionDef).

    Unlike a substring scan, this ignores names appearing in comments and
    string literals — eliminating the false-positive class where a function
    `get` would match any test mentioning the word "get" in prose. Falls
    back to substring matching if the test isn't valid Python.
    """
    try:
        tree = ast.parse(test_content)
    except SyntaxError:
        # Fall back: caller will substring-match. Sentinel: empty set means
        # "AST unavailable" — we encode that as None upstream via the wrapper.
        return frozenset()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.alias):
            names.add(node.asname or node.name.rsplit(".", 1)[-1])
    return frozenset(names)


def _sig_changed(before: ast.FunctionDef, after: ast.FunctionDef) -> bool:
    before_args = [a.arg for a in before.args.args]
    after_args = [a.arg for a in after.args.args]
    if before_args != after_args:
        return True
    return len(before.args.defaults) != len(after.args.defaults)


def _body_lines(fn: ast.FunctionDef) -> int:
    if fn.body and fn.body[-1].end_lineno is not None and fn.body[0].lineno is not None:
        return fn.body[-1].end_lineno - fn.body[0].lineno + 1
    return len(fn.body)


def _branch_count(fn: ast.FunctionDef) -> int:
    count = 0
    for node in ast.walk(fn):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.Try)):
            count += 1
    return count


def _behavior_changed(before: ast.FunctionDef, after: ast.FunctionDef) -> bool:
    bl = _body_lines(before)
    al = _body_lines(after)
    if bl == 0:
        return al > 0
    delta = abs(al - bl) / bl
    if delta > _BEHAVIOR_DELTA_THRESHOLD:
        return True
    return _branch_count(after) > _branch_count(before)


def classify_impact(
    source_before: str,
    source_after: str,
    test_content: str,
    test_file: Path,
    source_extension: str = ".py",
) -> ImpactReport:
    """Classify the impact of the source change on the test."""
    if source_extension != ".py":
        return ImpactReport(test_file, frozenset({ImpactClass.BEHAVIOR}), ())

    before = _collect_functions(source_before)
    after = _collect_functions(source_after)

    impacts: set[ImpactClass] = set()
    affected: list[str] = []

    common = set(before) & set(after)
    for name in common:
        bfn = before[name]
        afn = after[name]
        if _sig_changed(bfn, afn):
            impacts.add(ImpactClass.SIGNATURE)
            if name not in affected:
                affected.append(name)
        if _behavior_changed(bfn, afn):
            impacts.add(ImpactClass.BEHAVIOR)
            if name not in affected:
                affected.append(name)

    if set(after) - set(before):
        impacts.add(ImpactClass.COVERAGE)
    if set(before) - set(after):
        impacts.add(ImpactClass.COVERAGE)
        for removed in set(before) - set(after):
            if removed not in affected:
                affected.append(removed)

    # Filter affected_functions to ones the test actually references.
    # AST traversal beats `f in test_content`: it ignores names inside comments
    # and string literals, and matches whole identifiers (not substrings).
    # Falls back to substring match if the test file is not valid Python.
    try:
        ast.parse(test_content)
    except SyntaxError:
        affected = [f for f in affected if f in test_content]
    else:
        referenced = _names_referenced_in_test(test_content)
        affected = [f for f in affected if f in referenced]

    return ImpactReport(test_file, frozenset(impacts), tuple(affected))


# ---------------------------------------------------------------------------
# TM-2: symbol extraction and ChangedSet building
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SymbolSpan:
    """A named symbol and the 1-based line range it occupies."""

    name: str
    start_line: int
    end_line: int


def _collect_symbol_spans(source: str) -> dict[str, _SymbolSpan]:
    """Extract top-level and nested function/class symbols with line spans.

    Returns ``{name: _SymbolSpan}`` for every ``FunctionDef``,
    ``AsyncFunctionDef``, and ``ClassDef`` found in *source*.  When names
    collide (overloads, nested defs), the last one wins — stable enough for
    seed purposes.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, _SymbolSpan] = {}
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            end = getattr(node, "end_lineno", node.lineno)
            out[node.name] = _SymbolSpan(
                name=node.name,
                start_line=node.lineno,
                end_line=end if end is not None else node.lineno,
            )
    return out


def _diff_symbols(
    source_before: str,
    source_after: str,
) -> dict[str, ImpactClass]:
    """Map changed/added/removed symbol names → their ImpactClass.

    Used to seed the impact engine with symbol-granularity context for
    per-(symbol, test) pairing.
    """
    before = _collect_symbol_spans(source_before)
    after = _collect_symbol_spans(source_after)

    result: dict[str, ImpactClass] = {}

    # Added / removed → COVERAGE
    for name in set(after) - set(before):
        result[name] = ImpactClass.COVERAGE
    for name in set(before) - set(after):
        result[name] = ImpactClass.COVERAGE

    # Changed (common) → SIGNATURE or BEHAVIOR
    before_fns = _collect_functions(source_before)
    after_fns = _collect_functions(source_after)
    for name in set(before) & set(after):
        bfn = before_fns.get(name)
        afn = after_fns.get(name)
        if bfn is not None and afn is not None:
            if _sig_changed(bfn, afn):
                result[name] = ImpactClass.SIGNATURE
            elif _behavior_changed(bfn, afn):
                result[name] = ImpactClass.BEHAVIOR
            # else: no detectable change for this symbol
        else:
            # Node type is ClassDef or AsyncFunctionDef — count as behavior
            # change when line range differs
            bs = before[name]
            as_ = after[name]
            if (as_.end_line - as_.start_line) != (bs.end_line - bs.start_line):
                result[name] = ImpactClass.BEHAVIOR

    return result


def _parse_hunk_range(part: str) -> tuple[int, int]:
    """Parse a hunk side spec (``start`` or ``start,count``) → (start, count)."""
    if "," in part:
        start_s, count_s = part.split(",", 1)
        return int(start_s), int(count_s)
    return int(part), 1


def build_changed_set(
    project_root: Path,
    diff_against: str,
) -> ChangedSet:
    """Parse ``git diff`` hunk headers → :class:`ChangedSet` (path → line numbers).

    The +lines of each hunk (added/changed lines in the working tree) are
    recorded as 1-based **new-side** line numbers under the repo-relative
    POSIX path (``ChangedSet.lines``). Additively (S2 CP-1, #918), the -lines
    of each hunk (removed/replaced lines in the base tree) are recorded as
    1-based **base-side** line numbers (``ChangedSet.base_lines``) — the
    coordinate space the cached base coverage index is indexed in. The hunk
    header ``@@ -old_start[,old_count] +new_start[,new_count] @@`` carries both
    ranges, so a single pass populates both sides. Deleted-only hunks (no
    ``+`` lines) contribute an empty new-side frozenset for the path so callers
    still know the file was touched; pure additions leave ``base_lines`` empty
    for that path.

    Returns an empty ``ChangedSet`` when git is unavailable or the diff is
    clean.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from primitives._sdet.stacks.contract import ChangedSet as _ChangedSet  # noqa: PLC0415

    result = subprocess.run(  # noqa: S603
        ["git", "diff", f"{diff_against}...HEAD", "--unified=0"],  # noqa: S607
        cwd=_Path(str(project_root)),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return _ChangedSet()

    lines_by_path: dict[str, set[int]] = {}
    base_lines_by_path: dict[str, set[int]] = {}
    current_path: str | None = None
    current_hunk_count = 0
    current_line = 0
    current_base_count = 0
    current_base_line = 0

    for raw_line in result.stdout.splitlines():
        # diff --git a/... b/...  → new file section
        if raw_line.startswith("diff --git "):
            current_path = None
            continue

        # +++ b/path  → the after-file path
        if raw_line.startswith("+++ b/"):
            path_str = raw_line[6:]
            current_path = path_str  # repo-relative POSIX
            if current_path not in lines_by_path:
                lines_by_path[current_path] = set()
            if current_path not in base_lines_by_path:
                base_lines_by_path[current_path] = set()
            continue

        # Skip the --- a/... line
        if raw_line.startswith("--- "):
            continue

        # Hunk header: @@ -old_start[,old_count] +new_start[,new_count] @@
        if raw_line.startswith("@@") and current_path is not None:
            try:
                current_base_line, current_base_count = _parse_hunk_range(
                    raw_line.split("-")[1].split()[0]
                )
            except (IndexError, ValueError):
                current_base_count = 0
                current_base_line = 0
            with contextlib.suppress(IndexError, ValueError):
                current_line, current_hunk_count = _parse_hunk_range(
                    raw_line.split("+")[1].split()[0]
                )
            continue

        # Context / added / removed lines within a hunk
        if current_path is not None and raw_line:
            first = raw_line[0]
            if first == "+":
                # Added / changed line in the new file
                if current_hunk_count > 0:
                    lines_by_path[current_path].add(current_line)
                current_line += 1
            elif first == "-":
                # Removed / replaced line in the base (old) file
                if current_base_count > 0:
                    base_lines_by_path[current_path].add(current_base_line)
                current_base_line += 1
            elif first == " ":
                # Context line — advances both counters
                current_line += 1
                current_base_line += 1

    return _ChangedSet(
        lines={path: frozenset(lns) for path, lns in lines_by_path.items()},
        base_lines={path: frozenset(lns) for path, lns in base_lines_by_path.items()},
    )


# ---------------------------------------------------------------------------
# TM-2: coverage-primary impact engine
# ---------------------------------------------------------------------------


def _warn_static_fallback(project_root: Path, adapter: Any) -> None:
    """Log the engine-level degradation warning (S2 CP-4, #921).

    Emitted when a stack's ``coverage_map`` returns ``measured=False`` (cap
    exceeded, runner/tooling missing, cold over-cap, unparseable output). The
    per-stack ``coverage_map`` already logged *why* it could not measure; this
    is the single engine-level signal that the static import-reachability
    selection is therefore taking over — for every stack (JS/TS, Java, Python),
    with no fabricated per-test attribution and no hang.
    """
    _logger.warning(
        "coverage map unmeasured for %s (%s adapter); falling back to static "
        "import-reachability selection (source='static'). No per-test coverage "
        "attribution is fabricated.",
        project_root,
        getattr(adapter, "name", type(adapter).__name__),
    )


def _coverage_symbol_seed(
    all_symbols: dict[str, ImpactClass],
    changed_files: list[Path],
    symbol_impact_by_file: dict[Any, dict[str, ImpactClass]],
) -> dict[str, ImpactClass]:
    """Symbols to pair with empirically-impacted tests in the coverage leg.

    The Python-AST symbol diff only extracts symbols for ``.py`` files; for
    JS/TS + Java changed files ``all_symbols`` is empty, which would silently
    drop every coverage-index-selected (file, test) pair on those stacks (S2
    CP-4, #921). Mirror the static leg's file-level sentinel: when a changed
    file contributed no symbols, seed one file-name sentinel for it so a
    measured non-Python coverage map still surfaces ``source="coverage"``
    entries. Files that DID contribute Python symbols keep their precise set,
    so the Python path is byte-for-byte unchanged.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    seed: dict[str, ImpactClass] = dict(all_symbols)
    for cf in changed_files:
        if cf not in symbol_impact_by_file:
            seed.setdefault(_Path(str(cf)).name, ImpactClass.BEHAVIOR)
    return seed


def _augment_static(
    *,
    project_root: Path,
    changed_files: list[Path],
    symbol_impact_by_file: dict[Any, dict[str, ImpactClass]],
    pairs: dict[tuple[str, str], str],
) -> None:
    """Union the static import-reachability dep-graph walk into *pairs*.

    The multi-stack dep-graph (Python / JS-TS / Java) maps each changed source
    file → the test files that statically reach it. Each (symbol, test) pair is
    added with ``source="static"`` unless an equal-keyed coverage pair already
    won (coverage > static). Files with no extractable symbols use the
    file-name sentinel, matching :func:`_coverage_symbol_seed`. Mutates *pairs*
    in place.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from primitives._sdet.depgraph import find_affected_tests  # noqa: PLC0415

    static_map = find_affected_tests([_Path(str(f)) for f in changed_files], project_root)
    for cf, test_paths in static_map.items():
        sym_map = symbol_impact_by_file.get(cf, {})
        if not sym_map:
            sym_map = {_Path(str(cf)).name: ImpactClass.BEHAVIOR}
        for sym in sym_map:
            for tp in sorted(test_paths):
                try:
                    tid = _Path(str(tp)).resolve().relative_to(project_root).as_posix()
                except ValueError:
                    tid = _Path(str(tp)).as_posix()
                pairs.setdefault((sym, tid), "static")


def compute_impacted(
    project_root: Path,
    changed_files: list[Path],
    diff_against: str,
    *,
    adapter: Any | None = None,
    changed_set: ChangedSet | None = None,
) -> list[ImpactEntry]:
    """Compute the impacted (symbol, test) pairs for the given diff.

    Algorithm (spec §4 — coverage-primary hybrid, extended by TM-3):

    1. For each changed source file, diff its before/after to extract the
       changed/added/removed symbols and their :class:`ImpactClass`.
    2. Build a :class:`ChangedSet` from the git diff hunks (or use the
       supplied *changed_set*).
    3. **Coverage-primary:** if *adapter* is not None, call
       ``adapter.coverage_map(root, changed_set)`` and use
       ``CoverageMap.impacted_by(changed_set)`` as the empirical test set.
       Mark each (symbol, test) pair with ``source="coverage"``.
    4. **Static fallback / augment:** when ``CoverageMap.measured is False``,
       or no adapter is available, or a changed symbol still has no covering
       test, run the dep-graph walk (``find_affected_tests``) and union its
       results.  Mark each new pair with ``source="static"``.
    5. **Call-graph augment (TM-3):** call ``adapter.call_graph(root, syms)``
       (when adapter is not None); follow transitive *callers* via BFS;
       find tests reaching those callers via the dep-graph walk; union them
       with ``source="static-callgraph"``.  When ``call_graph`` returns
       ``None`` (JS/TS, Java, or Python failure), this step is silently
       skipped.
    6. **Gap detection (TM-3):** changed symbols with *no* (symbol, test)
       pair in the final set are emitted as gap entries
       (``source="gap"``, ``test_id=""``).
    7. Deduplicate by ``(symbol, test_id)``: coverage > static-callgraph >
       static.
    8. Return a deterministically-sorted ``list[ImpactEntry]`` (by symbol,
       then test_id; gap entries sort last within the same symbol).

    Args:
        project_root: Absolute root of the repo.
        changed_files: Source files (non-test) that changed in the diff.
        diff_against: Git ref (e.g. ``"HEAD~1"``, ``"main"``) to diff against.
        adapter: A :class:`~primitives._sdet.stacks.contract.TestStackAdapter`
            instance (or any object exposing ``.coverage_map``); ``None`` to
            skip the coverage-primary and call-graph legs.
        changed_set: Pre-built :class:`ChangedSet`; built from git if omitted.

    Returns:
        A stable-sorted ``list[ImpactEntry]`` (by symbol, then test_id).
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    from primitives._sdet.stacks.contract import SymbolSet  # noqa: PLC0415
    from primitives.test_maintainer.shared.callgraph import (  # noqa: PLC0415
        augment_with_callgraph,
        build_gap_entries,
        compute_gaps,
    )

    project_root = _Path(str(project_root)).resolve()

    # --- Step 0: build ChangedSet if not supplied ---
    if changed_set is None:
        changed_set = build_changed_set(project_root, diff_against)

    # --- Step 1: symbol diff per changed file ---
    # Maps file → {symbol: ImpactClass}
    symbol_impact_by_file: dict[_Path, dict[str, ImpactClass]] = {}
    for cf in changed_files:
        before_src = _git_show_before(project_root, diff_against, cf)
        try:
            after_src = _Path(str(cf)).read_text(encoding="utf-8", errors="replace")
        except OSError:
            after_src = ""
        syms = _diff_symbols(before_src, after_src)
        if syms:
            symbol_impact_by_file[cf] = syms

    # All changed symbols across all files
    # symbol -> ImpactClass (last file wins on collision, acceptable for seeding)
    all_symbols: dict[str, ImpactClass] = {}
    for sym_map in symbol_impact_by_file.values():
        all_symbols.update(sym_map)

    # Coverage-leg symbol seed (S2 CP-4, #921): precise Python symbols plus a
    # file-level sentinel for non-Python changed files (see helper docstring).
    coverage_symbols = _coverage_symbol_seed(all_symbols, changed_files, symbol_impact_by_file)

    # --- Step 2: coverage-primary leg ---
    # pairs: (symbol, test_id) -> source
    pairs: dict[tuple[str, str], str] = {}

    use_static = True

    if adapter is not None:
        cov_map: Any = adapter.coverage_map(project_root, changed_set)

        # Duck-type: any object with .measured and .impacted_by() is usable
        # (the real CoverageMap and test fakes both satisfy this).
        measured: bool = getattr(cov_map, "measured", False)
        if not measured:
            # End-to-end cap / runner-missing / cold-over-cap degradation (S2
            # CP-4, #921): one clear engine-level warning, then the deterministic
            # static graph below. No partial measurement, no hang. Uniform
            # across JS/TS, Java, and Python adapters (any ``measured=False``).
            _warn_static_fallback(project_root, adapter)
        if measured:
            # Empirically impacted tests, selected on the map's own coordinate
            # side (S2 CP-2, #919). A live map (Python's ``_read_coverage``)
            # carries ``side="new"`` → intersect new-side diff lines; an
            # index-materialised map (JS/TS, Java) carries ``side="base"`` →
            # intersect base-side diff lines. We only pass ``side=`` when the
            # map declares a base side: the historical new-side call signature
            # (``impacted_by(changed_set)``) is preserved byte-for-byte for the
            # Python adapter and any duck-typed map/fake that predates the
            # ``side`` parameter.
            cov_side: Literal["new", "base"] = getattr(cov_map, "side", "new")
            empirical_tests: frozenset[str] = (
                cov_map.impacted_by(changed_set, side="base")
                if cov_side == "base"
                else cov_map.impacted_by(changed_set)
            )
            # Pair every coverage-leg symbol (precise Python symbols, or the
            # file-level sentinel for non-Python stacks) with every empirically
            # impacted test. This is what makes the index-backed JS/TS + Java
            # maps actually surface ``source="coverage"`` entries (CP-4).
            for sym in coverage_symbols:
                for tid in sorted(empirical_tests):
                    key = (sym, tid)
                    if key not in pairs:
                        pairs[key] = "coverage"
            # Only fall back for symbols with NO covering test.
            uncovered_syms = {
                sym
                for sym in coverage_symbols
                if not any((sym, tid) in pairs for tid in empirical_tests)
            }
            use_static = len(uncovered_syms) > 0 or not empirical_tests
        # measured=False → fall back entirely to static

    # --- Step 3: static fallback / augment ---
    if use_static and changed_files:
        _augment_static(
            project_root=project_root,
            changed_files=changed_files,
            symbol_impact_by_file=symbol_impact_by_file,
            pairs=pairs,
        )

    # --- Step 4: call-graph augment (TM-3) ---
    # Gracefully skips when adapter is None or call_graph() returns None.
    symbols_set = SymbolSet(frozenset(all_symbols.keys()))
    pairs = augment_with_callgraph(
        project_root=project_root,
        changed_symbols=all_symbols,
        existing_pairs=pairs,
        adapter=adapter,
        symbols=symbols_set,
    )

    # --- Step 4b: declared-e2e augment (#1029) ---
    # A route/surface change flags the e2e specs the declared .foundry/e2e-map.yaml
    # associates with it (coverage maps don't reach browser e2e). A deterministic,
    # map-driven source leg; does not affect gap detection (an e2e-covered symbol is
    # not a gap — the surface glob is recorded as its symbol).
    from primitives.test_maintainer.shared.e2e_impact import (  # noqa: PLC0415
        compute_e2e_impacted,
    )

    if changed_set is not None:
        for e2e_entry in compute_e2e_impacted(project_root, changed_set):
            pairs.setdefault((e2e_entry.symbol, e2e_entry.test_id), e2e_entry.source)

    # --- Step 5: gap detection (TM-3) ---
    # Symbols with no (symbol, test) pair in the set are gap candidates.
    covered_syms = frozenset(sym for sym, _tid in pairs)
    gap_symbols = compute_gaps(
        changed_symbols=all_symbols,
        covered_symbols=covered_syms,
    )

    # --- Step 6: build ImpactEntry list, deduped, stable-sorted ---
    # Reconstruct impact_class from all_symbols or default to BEHAVIOR.
    entries: list[ImpactEntry] = []
    for (sym, tid), source in pairs.items():
        ic = all_symbols.get(sym, ImpactClass.BEHAVIOR)
        entries.append(ImpactEntry(symbol=sym, test_id=tid, impact_class=ic, source=source))

    # Append gap entries (test_id="", source="gap").
    entries.extend(build_gap_entries(gap_symbols=gap_symbols, changed_symbols=all_symbols))

    entries.sort(key=lambda e: (e.symbol, e.test_id))
    return entries


def write_impact_artifacts(
    run_dir: Path,
    entries: list[ImpactEntry],
    coverage_map: CoverageMap | None = None,
) -> None:
    """Write ``impacted.json`` and ``coverage-map.json`` under *run_dir*.

    ``impacted.json`` — list of per-(symbol, test) dicts, each carrying the
    ``source`` engine tag (``"coverage"`` when the measured coverage index/map
    selected the pair, ``"static"`` when the import-reachability fallback did,
    plus ``"static-callgraph"`` / ``"gap"``), so a consumer can tell which
    engine attributed each impacted entry (S2 CP-4, #921).
    ``coverage-map.json`` — serialised ``CoverageMap.per_test`` reverse index
    (test_id → [[path, line], ...]) if a measured map is available, tagged with
    the map's coordinate ``side`` (``"new"`` for a live Python map, ``"base"``
    for the index-materialised JS/TS + Java map); otherwise
    ``{"measured": false}``.
    """
    from pathlib import Path as _Path  # noqa: PLC0415

    _run_dir = _Path(str(run_dir))
    _run_dir.mkdir(parents=True, exist_ok=True)

    # impacted.json
    impacted_path = _run_dir / "impacted.json"
    impacted_path.write_text(
        json.dumps([e.to_dict() for e in entries], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # coverage-map.json
    cov_path = _run_dir / "coverage-map.json"
    if coverage_map is not None and getattr(coverage_map, "measured", False):
        per_test = getattr(coverage_map, "per_test", {})
        # ``side`` records which coordinate space the map was measured in so a
        # consumer auditing the run knows whether the impacted set came from a
        # live new-side map (Python) or the base-indexed map (JS/TS, Java).
        # Default to "new" for any duck-typed map/fake predating the field.
        serialised: dict[str, object] = {
            "measured": True,
            "side": getattr(coverage_map, "side", "new"),
            "per_test": {
                tid: sorted([path, line] for path, line in locs)
                for tid, locs in sorted(per_test.items())
            },
        }
    else:
        serialised = {"measured": False}

    cov_path.write_text(
        json.dumps(serialised, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _git_show_before(project_root: Path, diff_against: str, file: Path) -> str:
    """Return the content of *file* at *diff_against*, or empty string."""
    from pathlib import Path as _Path  # noqa: PLC0415

    try:
        rel = _Path(str(file)).resolve().relative_to(_Path(str(project_root)))
    except ValueError:
        return ""
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{diff_against}:{rel.as_posix()}"],  # noqa: S607
        cwd=_Path(str(project_root)),
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""

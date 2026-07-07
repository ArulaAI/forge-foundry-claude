"""Cached, incremental per-stack coverage index (S2 CP-1, #918).

The closed-loop maintainer's impact engine is *coverage-primary*: a per-test
coverage map (``test → executed (file, line)``) is the source of truth for
catching indirectly-impacted tests. Producing that map by re-running coverage
on every PR is too expensive for JS/TS + Java (per-test-file isolation runs),
so the design spec
(``docs/superpowers/specs/2026-06-19-test-maintainer-coverage-parity-design.md``)
calls for a **cached + incremental coverage index**:

- Measure the whole suite once, cache it keyed by content hashes.
- On later main-side refreshes, re-measure only the test files whose own
  content **or** any covered-source content changed (staleness sweep).
- PR runs consume the cached base index **read-only**, intersecting it with
  the diff's *base-side* line numbers (``materialize_coverage_map``).

This module is the **foundation** (CP-1): the model, the atomic content-hash
cache, the ``refresh`` staleness sweep, the ``MeasureFn`` seam (so the
per-stack isolation-run builders in CP-2 / CP-3 plug in without this module
knowing any runner), the read-only ``materialize_coverage_map`` selector, and
the ``max_coverage_runs`` cap with an explicit over-cap signal
(:class:`CapExceededError`) the caller catches to fall back to ``measured=False``.

No real test runner is invoked here: the ``measure`` callable is injected.
The cache write is atomic (temp file + ``os.replace``) so a killed run never
corrupts the index.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from primitives._sdet.stacks.contract import ChangedSet, CoverageMap

#: Bump when the on-disk JSON shape changes incompatibly.
SCHEMA_VERSION = "1.0"

#: A ``(source_path, line)`` location, ``source_path`` POSIX + repo-relative.
CoveredLocations = frozenset[tuple[str, int]]


class MeasureFn(Protocol):
    """The per-stack isolation-run seam.

    Given the repo *root* and a stack-native *test_id* (test-file id for
    JS/TS, class id for Java, node-id-or-file for Python), return the set of
    ``(source_path, line)`` locations that test file executed. The concrete
    implementations (CP-2 jest/vitest, CP-3 maven/gradle JaCoCo) shell out to
    the real runner with coverage scoped to that one test file; the index
    layer never knows how. Tests inject a fake.

    Implementations must be deterministic and subprocess-safe. A runner-
    missing / parse-failure path is the implementation's concern; it returns
    an empty frozenset (never fabricates) or the caller catches and degrades
    to ``measured=False`` upstream.
    """

    def __call__(self, root: Path, test_id: str) -> CoveredLocations:
        """Measure covered locations for one test file."""
        ...


@dataclass(frozen=True)
class CoverageIndex:
    """The persisted, whole-suite per-test coverage index for one stack.

    ``per_test_file`` maps a stack-native test-file id to the frozenset of
    ``(source_path, line)`` pairs it covered. ``manifest`` maps every tracked
    path (test files **and** covered sources) to its ``"sha256:<hex>"``
    content hash, the basis for staleness detection. Reflects the *base*
    (``main``) tree state; a PR run consumes it read-only.
    """

    stack: str
    per_test_file: dict[str, CoveredLocations] = field(default_factory=dict)
    manifest: dict[str, str] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Over-cap signal
# ---------------------------------------------------------------------------


class CapExceededError(RuntimeError):
    """Raised by :func:`refresh` when the staleness sweep would exceed the cap.

    The caller catches this and falls back to ``measured=False`` (static
    graph) rather than run hundreds of isolation invocations / hang CI. No
    measurement is performed and the index is left untouched when this raises.
    """

    def __init__(self, needed: int, cap: int) -> None:
        self.needed = needed
        self.cap = cap
        super().__init__(
            f"coverage-index refresh needs {needed} isolation run(s) but "
            f"max_coverage_runs={cap}; falling back to static selection"
        )


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


#: Stable sentinel hash for an absent file (so a deleted covered source marks
#: its dependents stale consistently and ``content_hash`` never raises).
_MISSING_HASH = "sha256:" + hashlib.sha256(b"\x00__forge_missing__\x00").hexdigest()


def content_hash(path: Path) -> str:
    """Return ``"sha256:<hex>"`` of *path*'s bytes; a stable sentinel if absent.

    Deterministic by construction (hash of the raw bytes). A missing file
    hashes to a fixed sentinel so callers can compare without special-casing
    ``FileNotFoundError`` at every site.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return _MISSING_HASH
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Cache load / save (atomic)
# ---------------------------------------------------------------------------


def default_index_path(project_root: Path, stack: str) -> Path:
    """The conventional (gitignored) cache location for *stack*'s index."""
    return project_root / ".foundry" / "test-maintainer" / "coverage-index" / f"{stack}.json"


def load_index(path: Path, *, stack: str = "") -> CoverageIndex:
    """Load a :class:`CoverageIndex` from *path*; empty index when absent.

    An absent cache is a cold start, not an error. When the file is missing
    the *stack* argument seeds the returned empty index's ``stack`` field
    (the on-disk file otherwise carries it).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return CoverageIndex(stack=stack)

    per_test_raw = raw.get("per_test_file", {})
    per_test: dict[str, CoveredLocations] = {
        tid: frozenset((str(p), int(ln)) for p, ln in locs) for tid, locs in per_test_raw.items()
    }
    return CoverageIndex(
        stack=str(raw.get("stack", stack)),
        per_test_file=per_test,
        manifest={str(k): str(v) for k, v in raw.get("manifest", {}).items()},
        schema_version=str(raw.get("schema_version", SCHEMA_VERSION)),
    )


def save_index(index: CoverageIndex, path: Path) -> None:
    """Persist *index* to *path* atomically and durably (temp file + rename).

    A killed run never leaves a half-written cache: the JSON is written to a
    temp file in the same directory, flushed + ``fsync``-ed to disk, then
    atomically renamed over the target. The ``fsync`` before the rename means a
    crash can't leave the cache absent (the bytes are durable before the
    rename makes them visible).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": index.schema_version,
        "stack": index.stack,
        "per_test_file": {
            tid: sorted([p, ln] for p, ln in locs)
            for tid, locs in sorted(index.per_test_file.items())
        },
        "manifest": dict(sorted(index.manifest.items())),
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            # Force the bytes to disk before the rename so a crash can't leave
            # the cache absent (atomic *and* durable).
            fh.flush()
            os.fsync(fh.fileno())
        # Atomic rename over the target (Path.replace wraps os.replace).
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# Refresh — incremental staleness sweep
# ---------------------------------------------------------------------------


def _is_stale(
    test_id: str,
    index: CoverageIndex,
    root: Path,
) -> bool:
    """A test file is stale iff its own hash or any covered-source hash changed.

    A test file new to the tree (not in ``per_test_file``) is stale too.
    """
    if test_id not in index.per_test_file:
        return True
    # Test file's own content changed?
    if index.manifest.get(test_id) != content_hash(root / test_id):
        return True
    # Any covered source's content changed?
    for src_path, _line in index.per_test_file[test_id]:
        if index.manifest.get(src_path) != content_hash(root / src_path):
            return True
    return False


def refresh(
    index: CoverageIndex,
    root: Path,
    *,
    test_files: list[str],
    measure: MeasureFn,
    max_runs: int,
) -> CoverageIndex:
    """Bring *index* up to date with the current tree, incrementally.

    A test file is **stale** iff its own content-hash **or** any covered-source
    content-hash changed (and a test file new to the tree is stale). Stale test
    files are re-measured via *measure* (one call each); unchanged ones are
    reused verbatim. Test files no longer in *test_files* are dropped. The
    returned index's ``manifest`` is rebuilt to reflect exactly the current
    ``test_files`` + their covered sources.

    Raises :class:`CapExceededError` (measuring nothing, leaving *index*
    untouched) when the number of stale/new files exceeds *max_runs* — the
    caller falls back to ``measured=False``.
    """
    stale = [tid for tid in test_files if _is_stale(tid, index, root)]

    if len(stale) > max_runs:
        raise CapExceededError(needed=len(stale), cap=max_runs)

    # Start from the reusable (non-stale) entries, scoped to the current set.
    keep = set(test_files)
    new_per_test: dict[str, CoveredLocations] = {
        tid: locs for tid, locs in index.per_test_file.items() if tid in keep and tid not in stale
    }

    # Re-measure stale/new test files.
    for tid in stale:
        new_per_test[tid] = measure(root, tid)

    # Rebuild the manifest from exactly the current test files + their sources.
    manifest: dict[str, str] = {}
    for tid, locs in new_per_test.items():
        manifest[tid] = content_hash(root / tid)
        for src_path, _line in locs:
            if src_path not in manifest:
                manifest[src_path] = content_hash(root / src_path)

    return replace(index, per_test_file=new_per_test, manifest=manifest)


# ---------------------------------------------------------------------------
# Read-only PR selection
# ---------------------------------------------------------------------------


def materialize_coverage_map(index: CoverageIndex, changed: ChangedSet) -> CoverageMap:
    """Materialise a ``measured=True`` :class:`CoverageMap` from *index*.

    Read-only: no measurement. The returned map's ``per_test`` is the index's
    per-test-file reverse index as-is, tagged ``side="base"`` — because the
    index is indexed in **base-side** coordinates, this is the sole base-side
    :class:`CoverageMap` producer. The impact engine selects it with
    ``CoverageMap.impacted_by(changed, side="base")`` (reading
    ``ChangedSet.base_lines``), the *only* place base-side lines are consumed;
    live ``measured=True`` maps stay ``side="new"`` and are never intersected
    against base lines. An empty index still yields ``measured=True`` (it
    simply selects no tests) — distinct from the runner-missing
    ``measured=False`` fallback the caller produces when the cache can't be
    built.
    """
    _ = changed  # selection is the caller's impacted_by() call; signature documents intent
    return CoverageMap(per_test=dict(index.per_test_file), measured=True, side="base")


__all__ = [
    "SCHEMA_VERSION",
    "CapExceededError",
    "CoverageIndex",
    "CoveredLocations",
    "MeasureFn",
    "content_hash",
    "default_index_path",
    "load_index",
    "materialize_coverage_map",
    "refresh",
    "save_index",
]

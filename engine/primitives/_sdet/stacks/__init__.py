"""Stack-detector plugin registry (FLEX-2, #174).

Each language stack lives in its own module under this package and exposes a
``detect_<lang>_stack(project_root) -> StackProfile | None`` function. The
modules self-register on import below, so anything that does
``from primitives._sdet.stacks import all_stacks`` sees every built-in stack
without name-importing each one. A consumer can ship a Rust/Go/etc. stack by
calling :func:`register_stack` from their own package's import path.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — kept at runtime per #1156 design
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from primitives._sdet.stacks.contract import TestStackAdapter

    DetectFn = Callable[[Path], Any]

_REGISTRY: dict[str, Any] = {}

#: Execution adapters keyed by stack name, in resolution priority order
#: (Python first — the first cut, design spec §8). Populated lazily by
#: :func:`_adapters` so the registry import stays side-effect-light.
_ADAPTER_ORDER: tuple[str, ...] = ("python", "nodejs", "java")  # +nodejs/java


def register_stack(name: str, detect_fn: Any) -> None:
    """Register a stack detector under *name* (e.g. ``"python"``, ``"java"``).

    The most recent registration wins so a plugin can override a built-in if
    they really need to. :func:`all_stacks` returns a shallow copy so callers
    can't accidentally mutate the registry.
    """
    _REGISTRY[name] = detect_fn


def all_stacks() -> dict[str, Any]:
    """Every registered stack detector keyed by name (alphabetical)."""
    return dict(sorted(_REGISTRY.items()))


#: S3 unit-test generation metadata keyed by stack name (#1110). Mirrors
#: ``_REGISTRY`` so a consumer (or the dotnet/react/angular adapters in
#: #1111/#1112) registers its own without forking the core.
_UNIT_META: dict[str, Any] = {}


def register_unit_meta(name: str, meta: Any) -> None:
    """Register the :class:`~primitives._sdet.stacks.contract.UnitTestMeta` for *name*.

    Most-recent registration wins (a plugin may override a built-in), matching
    :func:`register_stack`.
    """
    _UNIT_META[name] = meta


def unit_test_meta(name: str) -> Any:
    """The :class:`UnitTestMeta` registered under *name*, or ``None`` if unknown."""
    return _UNIT_META.get(name)


def all_unit_meta() -> dict[str, Any]:
    """Every registered unit-test metadata entry keyed by name (alphabetical)."""
    return dict(sorted(_UNIT_META.items()))


#: Data-backend (database engine) metadata keyed by backend name (#1124).
#: Mirrors ``_UNIT_META`` so a consumer registers its own DB engine without
#: forking the core. Populated by ``data_backends.py`` (DT-2.2) on import.
_DATA_BACKENDS: dict[str, Any] = {}


def register_data_backend(name: str, meta: Any) -> None:
    """Register the :class:`~primitives._sdet.stacks.contract.DataBackendMeta` for *name*.

    Most-recent registration wins (a plugin may override a built-in), matching
    :func:`register_stack` and :func:`register_unit_meta`.
    """
    _DATA_BACKENDS[name] = meta


def data_backend_meta(name: str) -> Any:
    """The :class:`DataBackendMeta` registered under *name*, or ``None`` if unknown."""
    return _DATA_BACKENDS.get(name)


def all_data_backends() -> dict[str, Any]:
    """Every registered data-backend metadata entry keyed by name (alphabetical)."""
    return dict(sorted(_DATA_BACKENDS.items()))


# ---------------------------------------------------------------------------
# Data-backend detection + override parsing (#1156)
# ---------------------------------------------------------------------------

#: Dependency-manifest globs scanned for data-backend driver signals (#1124).
_DATA_MANIFEST_GLOBS: tuple[str, ...] = (
    "pyproject.toml",
    "requirements*.txt",
    "package.json",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "*.csproj",
)

#: Max directory depth descended when scanning for monorepo sub-projects
#: (0 = root only; 2 catches apps/<x>/ and packages/<x>/ layouts).
_DATA_MAX_SCAN_DEPTH = 2

#: Directory names never descended into during the data-backend scan
#: (dependency / build / VCS trees). Hidden dirs are skipped by the leading-dot
#: rule in the walk, so only non-dot noise dirs are listed.
_DATA_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "bower_components",
        "vendor",
        "venv",
        "env",
        "__pycache__",
        "site-packages",
        "dist",
        "build",
        "target",
        "out",
        "coverage",
    }
)


def _iter_data_scan_dirs(project_root: Path, max_depth: int = _DATA_MAX_SCAN_DEPTH) -> list[Path]:
    """*project_root* plus each non-excluded subdirectory up to *max_depth*.

    Bounded breadth-first walk skipping dependency/build/VCS dirs, hidden dirs,
    and symlinked dirs (SEC-10 symlink-safety). Mirrors the monorepo sub-project
    walk so a JS/TS portal/ beside a Python root is scanned, not just the root.
    """
    root = project_root.resolve()
    dirs: list[Path] = [root]
    frontier: list[tuple[Path, int]] = [(root, 0)]
    while frontier:
        current, depth = frontier.pop()
        if depth >= max_depth:
            continue
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for child in entries:
            if not child.is_dir() or child.is_symlink():
                continue
            if child.name.startswith(".") or child.name in _DATA_SCAN_SKIP_DIRS:
                continue
            dirs.append(child)
            frontier.append((child, depth + 1))
    return dirs


def detect_data_backends(root: Path) -> list[str]:
    """Detect data backends from dependency manifests; sorted registered names (#1124).

    Scans *root* plus bounded sub-projects, reads each dependency manifest, and
    matches the per-backend driver ``signals`` from the registry
    (:func:`all_data_backends`). Returns a sorted list of registered backend
    names (empty when none match). The single source of truth shared by
    ``sdet_architect`` (DT-1.1) and ``test_scaffold`` (S4).
    """
    backends = all_data_backends()
    found: set[str] = set()
    for scan_dir in _iter_data_scan_dirs(root):
        for pattern in _DATA_MANIFEST_GLOBS:
            for manifest in sorted(scan_dir.glob(pattern)):
                if manifest.is_symlink() or not manifest.is_file():
                    continue
                try:
                    text = manifest.read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:
                    continue
                for name, meta in backends.items():
                    if any(sig in text for sig in meta.signals):
                        found.add(name)
    return sorted(found)


def parse_data_backends(value: str) -> list[str]:
    """Parse + validate a comma-separated ``--data-backend`` override (#1124).

    Rejects any name not in the registry (:class:`ValueError`) so a typo or an
    attacker-controlled value can never widen the closed set. Returns the valid
    names sorted + de-duplicated.
    """
    known = set(all_data_backends())
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    bad = [n for n in names if n not in known]
    if bad:
        raise ValueError(
            f"unknown --data-backend value(s) {bad!r}; valid: {', '.join(sorted(known))}"
        )
    return sorted(set(names))


def _adapters() -> dict[str, TestStackAdapter]:
    """Instantiate the execution adapters keyed by stack name.

    Lazy + local import so the detection registry has no hard dependency on
    the (subprocess-heavy) execution adapters. Only Python ships an adapter
    in TM-1 (#893); JS/TS + Java follow the same contract in TM-8/TM-9.
    """
    from primitives._sdet.stacks.java import JavaStackAdapter  # noqa: PLC0415
    from primitives._sdet.stacks.nodejs import NodeStackAdapter  # noqa: PLC0415
    from primitives._sdet.stacks.python import PythonStackAdapter  # noqa: PLC0415

    adapters: dict[str, TestStackAdapter] = {
        "python": PythonStackAdapter(),
        "nodejs": NodeStackAdapter(),  # TM-8 (#900)
        "java": JavaStackAdapter(),  # TM-9 (#901)
    }
    return adapters


def resolve_adapter(root: Path) -> TestStackAdapter | None:
    """Pick the best :class:`TestStackAdapter` for *root*, or ``None``.

    Walks :data:`_ADAPTER_ORDER` (Python first), keeping the adapter with
    the highest ``detect`` confidence; ties break on declaration order. A
    root no adapter recognises (all ``"low"``) resolves to ``None`` so
    callers can fall back to the static graph + the in-session model.
    """
    adapters = _adapters()
    rank: dict[str, int] = {"high": 3, "medium": 2, "low": 1}
    best: TestStackAdapter | None = None
    best_rank = 0
    for name in _ADAPTER_ORDER:
        adapter = adapters.get(name)
        if adapter is None:
            continue
        confidence = adapter.detect(root)
        if confidence == "low":
            continue
        score = rank[confidence]
        if score > best_rank:
            best, best_rank = adapter, score
    return best


# --- Built-in stacks self-register on import. ---------------------------------
# Late import so `register_stack` is defined first; each module ends with its
# own `register_stack(...)` call. F401 is suppressed because the imports are
# load-bearing for side effects (registration), not for name use.
# Playwright e2e adapter — opt-in (#1029): imported so it self-registers via
# register_stack, but deliberately NOT added to _ADAPTER_ORDER, so resolve_adapter
# never auto-selects it (e2e is slow/flaky; the maintainer resolves it explicitly).
from primitives._sdet.stacks import (  # noqa: F401, E402
    angular,
    data_backends,
    dotnet,
    java,
    nodejs,
    playwright,
    python,
    react,
)

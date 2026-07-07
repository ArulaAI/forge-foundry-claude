"""Shared source-file walk utilities with default scan exclusions.

All analysis/review primitives should walk ONLY application source trees,
not dependency directories (.venv/, node_modules/), VCS internals (.git/),
or emitted harness tooling (.foundry/, .claude/, and the emitted skill/rule
*subpaths* of .github/.agents/.agent/.cursor).

Public surface
--------------
DEFAULT_EXCLUDED_DIRS : frozenset[str]
    Directory *name* segments (not full paths) that are always excluded.
    Applied against every path component relative to the walk root.

VENDORED_DIRS : frozenset[str]
    Subset of DEFAULT_EXCLUDED_DIRS that contain emitted/vendored harness
    artifacts wholly (``.foundry``, ``.claude``).  These are re-included when
    ``include_vendored=True``.

DEFAULT_EXCLUDED_SUBPATHS : frozenset[str]
    Root-relative POSIX path *prefixes* (not whole dirs) for emitted harness
    skill/rule trees that live as subpaths of otherwise-legitimate dirs — e.g.
    ``.github/skills`` (so ``.github/workflows`` is still scanned).  A path is
    excluded if its root-relative POSIX path equals or is under one of these
    prefixes, on a proper path boundary (not a substring match).  Also
    re-included when ``include_vendored=True``.

DEFAULT_EXCLUDED_FILE_GLOBS : frozenset[str]
    Glob patterns matched against the *filename* (not full path).
    Applied in addition to directory exclusions.

is_excluded(path, root, *, include_vendored=False) -> bool
    Return True if *path* should be skipped during analysis.

walk_source_files(root, extensions, *, extra_excludes=frozenset(),
                  include_vendored=False) -> Iterator[Path]
    Yield source files under *root* matching *extensions*, sorted.
    Symlinked files are skipped (SEC-10 discipline).
"""

from __future__ import annotations

import fnmatch
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Dependency / build / cache dirs — never useful for application analysis.
_DEPENDENCY_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".eggs",
        "dist",
        "build",
        "target",
    }
)

# Wholly emitted harness / vendored tooling dirs.
# These are excluded by default but can be re-included via include_vendored=True.
# NOTE: only dirs that are *entirely* vendored belong here.  .github/.agents/
# .agent/.cursor hold the emitted skill/rule trees only as SUBPATHS (see
# DEFAULT_EXCLUDED_SUBPATHS) — they also hold legitimate config (workflows,
# CODEOWNERS, dependabot.yml, …) that analysis primitives want to scan.
VENDORED_DIRS: frozenset[str] = frozenset(
    {
        ".foundry",
        ".claude",
    }
)

# Root-relative POSIX path prefixes for the emitted harness skill/rule trees.
# Excluded by default; re-included via include_vendored=True.  Matched on a
# proper path boundary (equal-to or under), NOT as a substring.
DEFAULT_EXCLUDED_SUBPATHS: frozenset[str] = frozenset(
    {
        ".github/skills",  # Copilot emitted skills (.github also holds workflows/CODEOWNERS)
        ".agents/skills",  # Codex emitted skills
        ".agent/skills",  # Antigravity emitted skills
        ".cursor/rules",  # Cursor emitted rules
    }
)

# All directories excluded by default (whole-dir matches).
DEFAULT_EXCLUDED_DIRS: frozenset[str] = _DEPENDENCY_DIRS | VENDORED_DIRS

# File glob patterns matched against the filename only.
DEFAULT_EXCLUDED_FILE_GLOBS: frozenset[str] = frozenset(
    {
        "*.pyc",
        "*.egg-info",
        "*.pyo",
    }
)


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def _dir_is_excluded(
    part: str,
    *,
    include_vendored: bool,
    extra_excludes: frozenset[str],
) -> bool:
    """Return True if a directory *part* (path component) should be excluded."""
    effective = (
        _DEPENDENCY_DIRS | extra_excludes
        if include_vendored
        else DEFAULT_EXCLUDED_DIRS | extra_excludes
    )
    # Exact match
    if part in effective:
        return True
    # Glob match (e.g. "*.egg-info")
    return any("*" in pattern and fnmatch.fnmatch(part, pattern) for pattern in effective)


def _subpath_is_excluded(rel_posix: str) -> bool:
    """Return True if *rel_posix* equals or is under an excluded subpath prefix.

    Matched on a proper path boundary: ``.github/skills`` matches
    ``.github/skills`` and ``.github/skills/foo/SKILL.md`` but NOT
    ``.github/skills-extra`` or ``.github/workflows/ci.yml``.
    """
    return any(
        rel_posix == prefix or rel_posix.startswith(prefix + "/")
        for prefix in DEFAULT_EXCLUDED_SUBPATHS
    )


def _file_glob_excluded(name: str) -> bool:
    """Return True if the filename matches a DEFAULT_EXCLUDED_FILE_GLOBS pattern."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in DEFAULT_EXCLUDED_FILE_GLOBS)


def _traverses_symlink(path: Path, root: Path) -> bool:
    """Return True if any directory component between *root* and *path* is a symlink.

    Guards against following a symlinked *directory* into off-tree content
    (SEC-10): a file inside a symlinked dir is itself a real file, so the
    ``is_symlink()`` check on the file alone would miss it.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def is_excluded(
    path: Path,
    root: Path,
    *,
    include_vendored: bool = False,
    extra_excludes: frozenset[str] = frozenset(),
) -> bool:
    """Return True if *path* should be skipped during analysis walks.

    Args:
        path: Absolute path to check.
        root: Walk root; exclusion is applied to the relative path parts.
        include_vendored: When True, VENDORED_DIRS (.foundry, .claude) and the
            emitted-subpath trees (.github/skills, …) are allowed through.
            Dependency dirs (.venv, node_modules, …) are always excluded
            regardless of this flag.
        extra_excludes: Additional directory name segments to exclude beyond
            the defaults.

    Returns:
        True if the path should be skipped; False if it is safe to process.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        # Path outside root — treat as not excluded (caller handles)
        return False

    parts = relative.parts
    # Whole-dir exclusion: check every directory component except the filename.
    for part in parts[:-1]:
        if _dir_is_excluded(part, include_vendored=include_vendored, extra_excludes=extra_excludes):
            return True
    # Subpath exclusion: match the root-relative POSIX path against emitted
    # skill/rule prefixes (proper path-boundary match).
    if not include_vendored and _subpath_is_excluded(relative.as_posix()):
        return True
    # Filename glob exclusion.
    filename = parts[-1] if parts else ""
    return _file_glob_excluded(filename)


# ---------------------------------------------------------------------------
# Walk generator
# ---------------------------------------------------------------------------


def walk_source_files(
    root: Path,
    extensions: set[str],
    *,
    extra_excludes: frozenset[str] = frozenset(),
    include_vendored: bool = False,
) -> Iterator[Path]:
    """Yield source files under *root* matching *extensions*, sorted.

    Skips symlinked files (SEC-10 discipline).

    Args:
        root: Directory to walk (or a single file, returned directly if
            extension matches).
        extensions: Set of file suffix strings to include (e.g. {".py", ".java"}).
        extra_excludes: Additional directory name segments to exclude.
        include_vendored: When True, VENDORED_DIRS and emitted-subpath trees
            are allowed through.

    Yields:
        Absolute Path objects, sorted ascending.
    """
    if root.is_file():
        if root.suffix in extensions and not root.is_symlink():
            yield root
        return

    results: list[Path] = []
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        if f.is_symlink():
            continue
        # Skip files reached *through* a symlinked directory (SEC-10): walk the
        # intermediate path components between root and f and drop any that
        # traverse a symlink.
        if _traverses_symlink(f, root):
            continue
        if f.suffix not in extensions:
            continue
        if is_excluded(
            f,
            root,
            include_vendored=include_vendored,
            extra_excludes=extra_excludes,
        ):
            continue
        results.append(f)

    yield from sorted(results)

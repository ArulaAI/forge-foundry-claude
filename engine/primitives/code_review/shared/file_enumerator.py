"""C4 /code-review — File enumerator.

Walk code_path respecting --file-pattern glob, language auto-detect, and
always-excluded noise directories.
"""

from __future__ import annotations

import fnmatch
import sys
from typing import TYPE_CHECKING, Literal

from primitives._shared.scan_exclusions import DEFAULT_EXCLUDED_DIRS
from primitives._shared.scan_exclusions import is_excluded as _is_excluded

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Derive from the shared constant so .foundry/.claude/vendored dirs are excluded;
# add *.egg-info as an additional dir-glob this enumerator excludes (threaded
# into the shared is_excluded() via extra_excludes so the subpath rules also apply).
_EXTRA_EXCLUDES: frozenset[str] = frozenset({"*.egg-info"})
ALWAYS_EXCLUDED_DIRS: frozenset[str] = DEFAULT_EXCLUDED_DIRS | _EXTRA_EXCLUDES

LANGUAGE_EXTENSIONS: dict[str, list[str]] = {
    "python": [".py"],
    "ts": [".ts", ".tsx"],
    "java": [".java"],
    "auto": [".py", ".ts", ".tsx", ".java"],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _path_is_under_excluded_dir(path: Path, root: Path, *, include_vendored: bool = False) -> bool:
    """Return True if *path* is excluded relative to *root*.

    Delegates to the shared predicate so the whole-dir set, the emitted-subpath
    rules (.github/skills, …), and the include_vendored override all apply
    uniformly.  *.egg-info dirs are excluded via extra_excludes.
    """
    return _is_excluded(
        path, root, include_vendored=include_vendored, extra_excludes=_EXTRA_EXCLUDES
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enumerate_files(
    code_path: Path,
    language: Literal["python", "java", "ts", "auto"] = "auto",
    file_pattern: str | None = None,
    allowed_files: list[Path] | None = None,
    primitive_name: str = "code-review",
    *,
    include_vendored: bool = False,
) -> list[Path]:
    """Return sorted list of source files to review.

    Args:
        code_path: Directory or single file to scan.
        language: Language filter. 'auto' includes .py, .ts, .tsx, .java.
        file_pattern: Optional glob pattern to further filter (e.g. "src/*.py").
            Applied relative to code_path.
        allowed_files: If provided (from --git-diff), only files in this list
            that also pass other filters are returned.
        primitive_name: Slug of the invoking primitive, used in log labels
            (e.g. ``"code-review"``, ``"security-review"``).
            Defaults to ``"code-review"`` for backward-compatibility.

    Returns:
        Sorted list of Path objects for files to review.
    """
    if not code_path.exists():
        print(
            f"[{primitive_name}] enumerate_files: code_path does not exist: {code_path}",
            file=sys.stderr,
        )
        return []

    # Single file short-circuit
    if code_path.is_file():
        extensions = LANGUAGE_EXTENSIONS.get(language, LANGUAGE_EXTENSIONS["auto"])
        if code_path.suffix in extensions:
            return [code_path]
        return []

    extensions = LANGUAGE_EXTENSIONS.get(language, LANGUAGE_EXTENSIONS["auto"])
    candidates: list[Path] = []

    for path in sorted(code_path.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix not in extensions:
            continue
        if _path_is_under_excluded_dir(path, code_path, include_vendored=include_vendored):
            continue
        candidates.append(path)

    # Apply file_pattern glob filter
    if file_pattern is not None:
        filtered: list[Path] = []
        for path in candidates:
            try:
                rel = str(path.relative_to(code_path))
            except ValueError:
                rel = str(path)
            if fnmatch.fnmatch(rel, file_pattern) or fnmatch.fnmatch(path.name, file_pattern):
                filtered.append(path)
        candidates = filtered

    # Narrow to allowed_files (git-diff mode)
    if allowed_files is not None:
        allowed_set = {p.resolve() for p in allowed_files}
        candidates = [p for p in candidates if p.resolve() in allowed_set]

    print(
        f"[{primitive_name}] enumerate_files: {len(candidates)} file(s) selected "
        f"(language={language}, pattern={file_pattern!r})",
        file=sys.stderr,
    )
    return candidates

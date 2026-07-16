"""S70 /compliance-map — PR diff filter.

When --git-diff REF1...REF2 is given, narrows the code path scan to only
the files changed in the diff range. Runs `git diff --name-only REF1...REF2`
via subprocess and returns the list of changed file paths.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class GitDiffError(Exception):
    """Raised when the git diff command fails."""


# ---------------------------------------------------------------------------
# Diff-range validation (SEC-5 / #92)
# ---------------------------------------------------------------------------

# Valid git refs: alphanumerics + `_.~^@{}/:-`, optionally followed by a
# diff connector (`..` or `...`) and a second ref. Mirrors the safe subset
# of git's ref-format rules. The first character can't be `-` (so git
# can never interpret the value as a flag, even if argv order is reshuffled
# in a future refactor).
_VALID_REF_PART = r"[A-Za-z0-9_.~^@/:][A-Za-z0-9_.~^@{}/:-]*"
_VALID_DIFF_RANGE = re.compile(rf"^{_VALID_REF_PART}(\.\.\.?{_VALID_REF_PART})?$")


def validate_diff_range(value: str) -> str:
    """Validate that *value* is a safe git-diff range. Returns it unchanged.

    Rejects anything that could be interpreted by git as a flag (leading `-`)
    or contains shell-metacharacters / whitespace. Single refs, two-dot
    ranges (`A..B`), and three-dot ranges (`A...B`) are accepted.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"diff_range must be a non-empty string, got {value!r}")
    if not _VALID_DIFF_RANGE.fullmatch(value):
        raise ValueError(
            f"Invalid diff range {value!r}: must match a single git ref or a "
            "two/three-dot range of safe ref characters."
        )
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _resolve_repo_root(start: Path) -> Path:
    """Return the true git repository root containing *start*.

    ``git diff --name-only`` always emits paths relative to the repo TOPLEVEL,
    regardless of the subprocess cwd. So to turn those paths back into real
    on-disk files we must join them onto the toplevel — NOT onto whatever
    ``start`` path the caller happened to pass (which may be a subdirectory,
    e.g. ``api/``). Passing a subdir produced ``api/api/routes/x.py`` and every
    file was silently dropped (#1351). We resolve the toplevel via
    ``git rev-parse --show-toplevel`` from *start*; if that fails (not a git
    dir), we fall back to *start* so behaviour is unchanged for non-repo cwds.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=str(start),
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return start
    top = result.stdout.strip()
    if result.returncode == 0 and top:
        return Path(top)
    return start


def get_changed_files(
    diff_range: str,
    repo_root: Path | None = None,
    primitive_name: str = "compliance-map",
) -> list[Path]:
    """Return the list of files changed in *diff_range* (e.g. 'HEAD~1...HEAD').

    Args:
        diff_range: Git diff range in REF1...REF2 or REF1..REF2 format.
        repo_root: A path INSIDE the target git repository (typically the
            CODE_PATH). The true repository toplevel is resolved from it via
            ``git rev-parse --show-toplevel`` (#1351), so passing a subdirectory
            is fine — changed-file paths are joined onto the toplevel and then
            filtered to the CODE_PATH subtree by the caller's ``enumerate_files``.
            Defaults to cwd.
        primitive_name: Slug of the invoking primitive, used in log labels
            (e.g. ``"code-review"``, ``"security-review"``, ``"compliance-map"``).
            Defaults to ``"compliance-map"`` for backward-compatibility.

    Returns:
        Absolute :class:`Path` objects (under the repo toplevel) for the changed
        files that exist on disk.

    Raises:
        GitDiffError: If git command fails.
    """
    validate_diff_range(diff_range)
    start = repo_root or Path.cwd()
    # git diff --name-only reports paths relative to the repo TOPLEVEL, so both
    # the subprocess cwd and the path join must use the toplevel — not a subdir.
    cwd = _resolve_repo_root(start)

    try:
        result = subprocess.run(  # noqa: S603
            ["git", "diff", "--name-only", diff_range],  # noqa: S607
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitDiffError("git executable not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitDiffError(f"git diff timed out after 30s for range {diff_range!r}") from exc

    if result.returncode != 0:
        raise GitDiffError(
            f"git diff failed (exit {result.returncode}) for range {diff_range!r}: "
            f"{result.stderr.strip()}"
        )

    changed: list[Path] = []
    for raw_line in result.stdout.splitlines():
        stripped = raw_line.strip()
        if stripped:
            p = cwd / stripped
            if p.exists():
                changed.append(p)
            else:
                print(
                    f"[{primitive_name}] pr-diff: file in diff does not exist on disk: "
                    f"{stripped} — skipping",
                    file=sys.stderr,
                )

    print(
        f"[{primitive_name}] pr-diff: {len(changed)} changed file(s) in {diff_range!r}",
        file=sys.stderr,
    )
    return changed


def filter_to_changed_files(
    code_path: Path,
    diff_range: str,
    repo_root: Path | None = None,
    primitive_name: str = "compliance-map",
) -> Path | None:
    """Return a temporary view of *code_path* narrowed to files changed in *diff_range*.

    This creates a temporary directory symlinked to the changed files.
    For simplicity, we return the first changed file's parent that is within
    code_path, or code_path itself if no narrowing is possible.

    In practice, the orchestrator passes the changed files list to evidence_finder
    which iterates over them directly. This function returns code_path narrowed to
    only the directory subtree containing changed files.

    Args:
        code_path: Original code path.
        diff_range: Git diff range.
        repo_root: Repository root.
        primitive_name: Slug of the invoking primitive, used in log labels.
            Defaults to ``"compliance-map"`` for backward-compatibility.

    Returns:
        code_path (narrowing is handled by the orchestrator via changed_files list).
    """
    # The actual narrowing is done in the orchestrator which passes changed_files
    # to a wrapper that only scans those files. This function validates the diff range.
    changed = get_changed_files(diff_range, repo_root=repo_root, primitive_name=primitive_name)
    if not changed:
        print(
            f"[{primitive_name}] pr-diff: no changed files in {diff_range!r}; using full code_path",
            file=sys.stderr,
        )
    return code_path

from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING

from primitives.test_maintainer.shared.edit_proposal import EditProposal, Verdict

if TYPE_CHECKING:
    from pathlib import Path

_DEFAULT_COMMIT_MESSAGE = "test(maintainer): auto-update tests"

_TEST_NAME_PATTERNS = (
    re.compile(r"^test_.*\.py$"),
    re.compile(r".*_test\.py$"),
    re.compile(r".*\.(test|spec)\.(ts|tsx|js|jsx|mjs|cjs)$"),
    re.compile(r".*(Test|Tests|IT)\.java$"),
)
_TEST_DIR_PARTS = frozenset({"tests", "__tests__", "test"})


def _is_test_path(p: Path) -> bool:
    name = p.name
    for pat in _TEST_NAME_PATTERNS:
        if pat.match(name):
            return True
    if any(part in _TEST_DIR_PARTS for part in p.parts):
        return True
    return "src/test/java" in str(p).replace("\\", "/")


def _detect_ci_mode() -> bool:
    """Auto-detect CI environment via the canonical CI env var."""
    return os.environ.get("CI", "").lower() in ("true", "1", "yes")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def apply_proposals(
    project_root: Path,
    proposals: list[EditProposal],
    *,
    commit_message: str | None = None,
    ci_mode: bool | None = None,
) -> list[Path]:
    """Apply EditProposal list to disk: write/delete test files; optionally commit.

    Args:
        project_root: Project root (test files must be under this).
        proposals: Accepted proposals to apply.
        commit_message: Git commit message to use in CI mode. Default:
            'test(maintainer): auto-update tests'.
        ci_mode: True → commit. False → only stage. None → auto-detect from CI env var.

    Returns:
        List of paths that were written or deleted.

    Hard rule: never modifies files outside detected test directories or with
    non-test names. Raises ValueError if a proposal targets a non-test path.
    """
    project_root = project_root.resolve()
    touched: list[Path] = []

    # Hard-rule check FIRST — fail loud before any I/O
    for p in proposals:
        if not _is_test_path(p.test_file):
            raise ValueError(
                f"Refusing to write non-test path: {p.test_file}. "
                "test_maintainer is hard-restricted to test files only."
            )

    for p in proposals:
        target = p.test_file
        if not target.is_absolute():
            target = project_root / target

        if p.verdict == Verdict.DELETE:
            if target.is_file():
                target.unlink()
                touched.append(target)
            continue

        # UPDATE or ADD
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(p.after_content, encoding="utf-8")
        touched.append(target)

    if not touched:
        return touched

    is_ci = _detect_ci_mode() if ci_mode is None else ci_mode

    # Stage paths
    for touched_path in touched:
        rel = touched_path.relative_to(project_root) if touched_path.is_absolute() else touched_path
        _git(["add", str(rel)], project_root)

    if is_ci:
        msg = commit_message or _DEFAULT_COMMIT_MESSAGE
        result = _git(["commit", "-m", msg], project_root)
        if result.returncode != 0:
            # Don't fail loudly — staged but uncommitted is acceptable degradation
            pass

    return touched

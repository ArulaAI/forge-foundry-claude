"""Recent-deltas extractor — generates recent-deltas.md content.

Produces a structured markdown string covering:
  - Most-changed files (top 20 by commit-touch count)
  - Recent commits (top 15 by recency)
  - Recurring commit-subject prefixes (conventional-commits counts)

Output is truncated to config.budget.per_file.recent_deltas tokens via enforce_budget.
If project_root has no .git/, emits a single _Not a git repository._ line.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from typing import TYPE_CHECKING

from primitives.repo_digest.shared.budget import enforce_budget

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import DigestConfig

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOP_FILES = 20
_TOP_COMMITS = 15

# Conventional-commit prefixes to detect for the "Recurring patches" section
_CC_PREFIX_RE = re.compile(
    r"^(fix|feat|chore|docs|refactor|test|perf|build|ci|style)(\([^)]*\))?:",
)

# Sentinel that separates commits in the git log output
_COMMIT_SENTINEL = "COMMIT "


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _is_git_repo(project_root: Path) -> bool:
    """Return True if project_root contains a .git directory or file."""
    return (project_root / ".git").exists()


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git command and return stdout, or None on failure."""
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_git_log(raw: str) -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    """Parse git log output into commits and file-touch counts.

    Returns:
        commits: list of (short_hash, date, subject) in log order (most recent first)
        file_counts: mapping of file path -> number of commits touching it
    """
    commits: list[tuple[str, str, str]] = []
    file_counts: Counter[str] = Counter()

    current_hash: str | None = None
    current_date: str | None = None
    current_subject: str | None = None

    for line in raw.splitlines():
        if line.startswith(_COMMIT_SENTINEL):
            # Save previous commit if any
            all_set = current_hash is not None and current_date is not None
            if all_set and current_subject is not None:
                commits.append((current_hash, current_date, current_subject))  # type: ignore[arg-type]

            # Parse "COMMIT <hash> <date> <subject...>"
            rest = line[len(_COMMIT_SENTINEL) :]
            parts = rest.split(" ", 2)
            _FULL = 3
            _DATE_ONLY = 2
            if len(parts) >= _FULL:
                current_hash = parts[0]
                current_date = parts[1]
                current_subject = parts[2].strip()
            elif len(parts) == _DATE_ONLY:
                current_hash = parts[0]
                current_date = parts[1]
                current_subject = ""
            else:
                current_hash = parts[0] if parts else ""
                current_date = ""
                current_subject = ""
        elif line.strip() and current_hash is not None:
            # Non-empty, non-sentinel line after a commit header = file name
            file_counts[line.strip()] += 1

    # Flush last commit
    if current_hash is not None and current_date is not None and current_subject is not None:
        commits.append((current_hash, current_date, current_subject))

    return commits, dict(file_counts)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_most_changed_section(file_counts: dict[str, int]) -> list[str]:
    """Build the ## Most-changed files section."""
    lines: list[str] = ["## Most-changed files", ""]

    if not file_counts:
        lines.append("_No file changes recorded in this window._")
        lines.append("")
        return lines

    top = sorted(file_counts.items(), key=lambda kv: kv[1], reverse=True)[:_TOP_FILES]
    for filepath, count in top:
        noun = "change" if count == 1 else "changes"
        lines.append(f"- `{filepath}` ({count} {noun})")

    lines.append("")
    return lines


def _build_recent_commits_section(commits: list[tuple[str, str, str]]) -> list[str]:
    """Build the ## Recent commits section."""
    lines: list[str] = ["## Recent commits", ""]

    if not commits:
        lines.append("_No commits in this window._")
        lines.append("")
        return lines

    top = commits[:_TOP_COMMITS]
    for short_hash, date, subject in top:
        lines.append(f"- `{short_hash}` {date} — {subject}")

    lines.append("")
    return lines


def _build_recurring_patches_section(commits: list[tuple[str, str, str]]) -> list[str]:
    """Build the ## Recurring patches section."""
    lines: list[str] = ["## Recurring patches", ""]

    prefix_counts: Counter[str] = Counter()
    for _, _, subject in commits:
        m = _CC_PREFIX_RE.match(subject)
        if m:
            # e.g. "fix(payments)" or "feat"
            prefix = m.group(1) + (m.group(2) or "")
            prefix_counts[prefix] += 1

    if not prefix_counts:
        lines.append("_No recurring commit-subject prefixes detected._")
        lines.append("")
        return lines

    # Sort by count descending, then alphabetically
    for prefix, count in sorted(prefix_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        noun = "commit" if count == 1 else "commits"
        lines.append(f"- `{prefix}`: {count} {noun}")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_recent_deltas(
    project_root: Path,
    *,
    config: DigestConfig,
) -> str:
    """Generate recent-deltas.md content.

    Sections produced:
        # Recent deltas (last N days)
        ## Most-changed files
        - `src/foo.py` (12 changes)
        ## Recent commits
        - `abc1234` 2026-05-09 — Fix payment timeout retry logic
        ## Recurring patches
        - `fix(payments)`: 5 commits

    Returns `_Not a git repository._` when project_root has no .git/.
    Honors config.budget.per_file.recent_deltas token cap (uses enforce_budget).
    """
    if not _is_git_repo(project_root):
        return "_Not a git repository._"

    days = config.extractors.recent_deltas.days

    raw = _run_git(
        [
            "log",
            f"--since={days} days ago",
            "--name-only",
            "--pretty=format:COMMIT %h %ad %s",
            "--date=short",
        ],
        cwd=project_root,
    )

    commits: list[tuple[str, str, str]] = []
    file_counts: dict[str, int] = {}

    if raw is not None and raw.strip():
        commits, file_counts = _parse_git_log(raw)

    lines: list[str] = [
        f"# Recent deltas (last {days} days)",
        "",
    ]
    lines.extend(_build_most_changed_section(file_counts))
    lines.extend(_build_recent_commits_section(commits))
    lines.extend(_build_recurring_patches_section(commits))

    body = "\n".join(lines)
    return enforce_budget(body, config.budget.per_file.recent_deltas)

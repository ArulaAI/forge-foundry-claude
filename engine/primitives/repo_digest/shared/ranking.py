"""File-scoring heuristic for repo_digest: score_file() + rank_files().

Composite ranking combines:
- size_score:   LOC normalized against max file size in the candidate set
- recent_score: git commit count in last N days normalized against max
- entry_score:  1.0 if file has a recognised entry-point pattern; else 0.0
- api_score:    1.0 if file exposes any public API; else 0.0

Weights are configurable via RankingWeights (from DigestConfig).
"""

from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import RankingWeights

# ---------------------------------------------------------------------------
# Size helper
# ---------------------------------------------------------------------------


def _line_count(file: Path) -> int:
    try:
        return file.read_text(encoding="utf-8").count("\n") + 1
    except (UnicodeDecodeError, PermissionError, FileNotFoundError):
        return 0


# ---------------------------------------------------------------------------
# Recent-change helper
# ---------------------------------------------------------------------------


def _recent_change_count(file: Path, project_root: Path, days: int) -> int:
    """Count commits touching *file* in last *days* days. Returns 0 if not a git repo."""
    try:
        rel = file.relative_to(project_root)
    except ValueError:
        return 0
    result = subprocess.run(  # noqa: S603
        ["git", "log", f"--since={days} days ago", "--pretty=format:%H", "--", str(rel)],  # noqa: S607
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return len(lines)


# ---------------------------------------------------------------------------
# Entry-point detection
# ---------------------------------------------------------------------------

_ENTRY_PATTERNS = [
    # Python: if __name__ == "__main__":
    re.compile(r'^\s*if\s+__name__\s*==\s*["\']__main__["\']\s*:', re.MULTILINE),
    # JS/TS: export default [async] function ...
    re.compile(r"^\s*export\s+default\s+(?:async\s+)?function\b", re.MULTILINE),
    # Java: public static void main(
    re.compile(r"^\s*public\s+static\s+void\s+main\s*\(", re.MULTILINE),
]


def _has_entry_point(file: Path) -> bool:
    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, FileNotFoundError):
        return False
    return any(p.search(content) for p in _ENTRY_PATTERNS)


# ---------------------------------------------------------------------------
# Public-API detection
# ---------------------------------------------------------------------------

_API_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    ".py": [
        re.compile(r"^def\s+([a-zA-Z][\w]*)\s*\(", re.MULTILINE),
        re.compile(r"^class\s+([A-Z][\w]*)\s*[:\(]", re.MULTILINE),
    ],
    ".ts": [
        re.compile(
            r"^export\s+(?:async\s+)?(?:function|class|const|let|var|interface|type)\b",
            re.MULTILINE,
        ),
    ],
    ".tsx": [re.compile(r"^export\s+", re.MULTILINE)],
    ".js": [re.compile(r"^(?:export\s+|module\.exports\s*=)", re.MULTILINE)],
    ".jsx": [re.compile(r"^export\s+", re.MULTILINE)],
    ".java": [
        re.compile(r"^\s*public\s+(?:abstract\s+|final\s+)?class\s+[A-Z]", re.MULTILINE),
        re.compile(r"^\s*public\s+(?:static\s+|final\s+)*(?:\w+\s+)+\w+\s*\(", re.MULTILINE),
    ],
}


def _has_public_api(file: Path) -> bool:
    patterns = _API_PATTERNS.get(file.suffix)
    if patterns is None:
        return False
    try:
        content = file.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError, FileNotFoundError):
        return False
    for pat in patterns:
        for match in pat.finditer(content):
            if file.suffix == ".py":
                # Exclude underscore-prefixed names (private by convention)
                name = match.group(1)
                if not name.startswith("_"):
                    return True
            else:
                return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_file(
    file: Path,
    *,
    project_root: Path,
    weights: RankingWeights,
    max_size_lines: int,
    max_recent_count: int,
    recent_days: int = 30,
) -> float:
    """Return composite score in [0, 1] for *file*.

    Callers are responsible for computing max_size_lines and max_recent_count
    across the candidate file set so that normalization is consistent.
    """
    size = _line_count(file)
    recent = _recent_change_count(file, project_root, recent_days)

    size_score = size / max_size_lines if max_size_lines > 0 else 0.0
    recent_score = recent / max_recent_count if max_recent_count > 0 else 0.0
    entry = 1.0 if _has_entry_point(file) else 0.0
    api = 1.0 if _has_public_api(file) else 0.0

    return (
        weights.size * size_score
        + weights.recent * recent_score
        + weights.entry * entry
        + weights.api * api
    )


def rank_files(
    files: list[Path],
    *,
    project_root: Path,
    weights: RankingWeights,
    days: int = 30,
) -> list[tuple[Path, float]]:
    """Return *files* sorted by descending composite score with their scores."""
    if not files:
        return []

    sizes = [_line_count(f) for f in files]
    max_size = max(sizes) if sizes else 1

    recents = [_recent_change_count(f, project_root, days) for f in files]
    max_recent = max(recents) if recents else 1

    scored: list[tuple[Path, float]] = [
        (
            f,
            score_file(
                f,
                project_root=project_root,
                weights=weights,
                max_size_lines=max_size,
                max_recent_count=max_recent,
                recent_days=days,
            ),
        )
        for f in files
    ]
    scored.sort(key=lambda x: -x[1])
    return scored

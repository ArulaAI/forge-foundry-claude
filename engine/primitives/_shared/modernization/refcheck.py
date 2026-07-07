"""Validate-as-script for ``path:line`` references (Layer 1).

Generalized from ``mfx``'s ``validate.py`` (design spec §2 "Validation-as-script",
§3 Layer 1). A model writes a Markdown deliverable that cites evidence as
``path/to/file.ext:line`` (optionally a ``:start-end`` range). Instead of a
second model pass to "self-check" those citations, :func:`check` parses every
reference out of the deliverable and validates it against the real tree in pure
Python — the model then fixes only the flagged refs.

Security: the paths come from model-written text, so a reference is rejected
(``path_traversal``) before any filesystem access if it is absolute or escapes
``repo_root`` (``..``), mirroring the SEC-7/SEC-10 ``..``-before-``.resolve()``
discipline. ``check`` only ever reads files inside ``repo_root``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from pathlib import Path

#: Matches ``<path>.<ext>:<line>`` or ``<path>.<ext>:<start>-<end>``. The path
#: must carry a file extension (``.<alnum>``) immediately before ``:<digits>`` so
#: timestamps (``12:30``), ``key: value``, and ``http://host:port`` don't match.
#: Path chars are conservative: word chars, ``/``, ``.``, ``-`` (no spaces).
_REF_RE = re.compile(r"(?P<path>[\w./\-]+\.\w+):(?P<start>\d+)(?:-(?P<end>\d+))?")

BrokenReason = Literal[
    "file_not_found",
    "not_a_file",
    "line_out_of_range",
    "path_traversal",
    "bad_range",
]


class BrokenRef(BaseModel):
    """A single ``path:line`` reference that did not resolve.

    ``source_line`` is the 1-indexed line in the *deliverable* where the ref was
    written, so the model can jump straight to the citation it must fix.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str
    path: str
    line_start: int
    line_end: int | None
    reason: BrokenReason
    source_line: int


class RefCheckReport(BaseModel):
    """The ``validation.json`` result: every broken ref + the totals."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    repo_root: str
    total_refs: int = 0
    broken: list[BrokenRef] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when every parsed reference resolved."""
        return not self.broken


def _is_contained(repo_root: Path, rel: str) -> bool:
    """True if ``repo_root / rel`` stays under ``repo_root`` (reject ``..``/abs)."""
    # SEC discipline: reject on the RAW value before resolving.
    if rel.startswith("/") or ".." in rel.split("/"):
        return False
    return (repo_root / rel).resolve().is_relative_to(repo_root.resolve())


def _classify(
    repo_root: Path,
    path: str,
    start: int,
    end: int | None,
    *,
    _line_count_cache: dict[str, int],
    _stat_cache: dict[str, str],
) -> BrokenReason | None:
    """Return the break reason for one ref, or ``None`` when it resolves.

    *_line_count_cache* maps resolved path strings → line counts (populated on
    first access; avoids re-reading the same file for multiple refs).
    *_stat_cache* maps raw path strings → ``"ok" | "not_found" | "not_a_file" |
    "traversal"`` (populated on first access; avoids repeated exists/is_file
    and resolve syscalls for the same path).
    Both caches are per-:func:`check` call and must never outlive it.
    """
    if start < 1:
        return "line_out_of_range"  # lines are 1-indexed; ``:0`` is malformed
    if end is not None and end < start:
        return "bad_range"
    # ------------------------------------------------------------------
    # Stat cache: existence + file-type check (one resolve per unique path).
    # ------------------------------------------------------------------
    stat = _stat_cache.get(path)
    if stat is None:
        if not _is_contained(repo_root, path):
            stat = "traversal"
        else:
            target = repo_root / path
            if not target.exists():
                stat = "not_found"
            elif not target.is_file():
                stat = "not_a_file"
            else:
                stat = "ok"
        _stat_cache[path] = stat
    if stat == "traversal":
        return "path_traversal"
    if stat == "not_found":
        return "file_not_found"
    if stat == "not_a_file":
        return "not_a_file"
    # ------------------------------------------------------------------
    # Line-count cache: read file at most once per check() call.
    # ------------------------------------------------------------------
    line_count = _line_count_cache.get(path)
    if line_count is None:
        target = repo_root / path
        line_count = target.read_text(encoding="utf-8", errors="replace").count("\n") + 1
        _line_count_cache[path] = line_count
    if max(start, end or start) > line_count:
        return "line_out_of_range"
    return None


def check(deliverable_md: str, repo_root: Path) -> RefCheckReport:
    """Validate every ``path:line`` reference in *deliverable_md* against the tree.

    Args:
        deliverable_md: the Markdown deliverable the model wrote.
        repo_root: the root the references are relative to.

    Returns:
        A :class:`RefCheckReport` listing every broken reference (with the
        deliverable line it appears on) and the total reference count.
        ``report.ok`` is ``True`` when nothing is broken.
    """
    broken: list[BrokenRef] = []
    total = 0
    # Per-call caches so each unique file is read/stat'd at most once.
    _line_count_cache: dict[str, int] = {}
    _stat_cache: dict[str, str] = {}
    for source_line, text in enumerate(deliverable_md.splitlines(), start=1):
        for m in _REF_RE.finditer(text):
            total += 1
            path = m.group("path")
            start = int(m.group("start"))
            end = int(m.group("end")) if m.group("end") is not None else None
            reason = _classify(
                repo_root,
                path,
                start,
                end,
                _line_count_cache=_line_count_cache,
                _stat_cache=_stat_cache,
            )
            if reason is not None:
                broken.append(
                    BrokenRef(
                        ref=m.group(0),
                        path=path,
                        line_start=start,
                        line_end=end,
                        reason=reason,
                        source_line=source_line,
                    )
                )
    return RefCheckReport(repo_root=str(repo_root), total_refs=total, broken=broken)

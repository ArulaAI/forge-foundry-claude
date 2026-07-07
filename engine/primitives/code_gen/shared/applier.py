"""Pure file mutator for the C3 code_gen PLB primitive.

Applies a ChangeSet to a working tree: anchored find/replace edits (unique
match or AnchorError), creates (with implicit mkdir -p), deletes, and moves
(git mv when in a repo). Enforces path-confinement + the task allowlist. Does
NOT run tests, format, or talk to a model.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from primitives.code_gen.shared.changeset import (
    ChangeSet,
    CreateOp,
    DeleteOp,
    EditOp,
    MoveOp,
    Op,
)


class AnchorError(Exception):
    """An edit anchor matched zero times or more than once."""


@dataclass(frozen=True)
class DroppedOp:
    path: str
    reason: str


@dataclass
class ApplyReport:
    applied: list[str] = field(default_factory=list)
    dropped: list[DroppedOp] = field(default_factory=list)
    touched: set[str] = field(default_factory=set)


def _is_containment_safe(raw_path: str, root: Path) -> bool:
    if raw_path.startswith("-"):
        return False
    p = Path(raw_path)
    if p.is_absolute() or ".." in p.parts:
        return False
    try:
        (root / p).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _op_paths(op: Op) -> list[str]:
    if isinstance(op, MoveOp):
        return [op.src, op.dst]
    return [op.path]


def _apply_edit(root: Path, op: EditOp) -> None:
    target = root / op.path
    text = target.read_text()
    for edit in op.edits:
        count = text.count(edit.old)
        if count == 0:
            raise AnchorError(f"{op.path}: anchor has no match: {edit.old!r}")
        if count > 1:
            raise AnchorError(
                f"{op.path}: anchor has {count} matches (must be unique): {edit.old!r}"
            )
        text = text.replace(edit.old, edit.new, 1)
    target.write_text(text)


def apply_changeset(
    cs: ChangeSet, root: Path, allowlist: set[str], use_git: bool = True
) -> ApplyReport:
    report = ApplyReport()
    for op in cs.ops:
        paths = _op_paths(op)
        bad = next((p for p in paths if not _is_containment_safe(p, root)), None)
        if bad is not None:
            report.dropped.append(DroppedOp(bad, "containment violation"))
            continue
        out = next((p for p in paths if p not in allowlist), None)
        if out is not None:
            report.dropped.append(DroppedOp(out, "path not in task allowlist"))
            continue

        if isinstance(op, CreateOp):
            dest = root / op.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(op.content)
        elif isinstance(op, EditOp):
            _apply_edit(root, op)
        elif isinstance(op, DeleteOp):
            (root / op.path).unlink(missing_ok=True)
        elif isinstance(op, MoveOp):
            dest = root / op.dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            if use_git:
                subprocess.run(
                    ["git", "mv", op.src, op.dst],
                    cwd=root,
                    check=True,
                    timeout=60,
                )
            else:
                (root / op.src).rename(dest)

        report.applied.append(str(op))
        report.touched.update(paths)
    return report

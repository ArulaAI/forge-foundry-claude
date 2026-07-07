"""Change-set schema for the C3 code_gen PLB primitive.

The model's entire output surface: a JSON document of create/edit/delete/move
ops. This module is the single source of truth shared by the extract and
continue legs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


class ChangeSetError(ValueError):
    """Raised when a change-set document is malformed."""


@dataclass(frozen=True)
class Edit:
    old: str
    new: str


@dataclass(frozen=True)
class CreateOp:
    path: str
    content: str


@dataclass(frozen=True)
class EditOp:
    path: str
    edits: list[Edit]


@dataclass(frozen=True)
class DeleteOp:
    path: str


@dataclass(frozen=True)
class MoveOp:
    src: str
    dst: str


Op = CreateOp | EditOp | DeleteOp | MoveOp


@dataclass(frozen=True)
class ChangeSet:
    task_id: str
    ops: list[Op]
    notes: str | None = None


def _require(d: dict[str, object], key: str, where: str) -> object:
    if key not in d:
        raise ChangeSetError(f"{where}: missing required field {key!r}")
    return d[key]


def _parse_op(d: dict[str, object]) -> Op:
    kind = d.get("op")
    if kind == "create":
        return CreateOp(str(_require(d, "path", "create")), str(_require(d, "content", "create")))
    if kind == "edit":
        raw_edits = _require(d, "edits", "edit")
        if not isinstance(raw_edits, list) or not raw_edits:
            raise ChangeSetError("edit: 'edits' must be a non-empty list")
        edits = [
            Edit(str(_require(e, "old", "edit")), str(_require(e, "new", "edit")))
            for e in raw_edits
        ]
        return EditOp(str(_require(d, "path", "edit")), edits)
    if kind == "delete":
        return DeleteOp(str(_require(d, "path", "delete")))
    if kind == "move":
        return MoveOp(str(_require(d, "from", "move")), str(_require(d, "to", "move")))
    raise ChangeSetError(f"unknown op: {kind!r}")


def parse_changeset(raw: str | dict[str, object]) -> ChangeSet:
    doc = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(doc, dict):
        raise ChangeSetError("change-set must be a JSON object")
    task_id = str(_require(doc, "task_id", "change-set"))
    raw_ops = _require(doc, "ops", "change-set")
    if not isinstance(raw_ops, list):
        raise ChangeSetError("change-set: 'ops' must be a list")
    ops = [_parse_op(o) for o in raw_ops]
    notes = doc.get("notes")
    return ChangeSet(task_id=task_id, ops=ops, notes=str(notes) if notes is not None else None)

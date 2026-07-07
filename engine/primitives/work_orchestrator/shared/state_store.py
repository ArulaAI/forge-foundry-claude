"""C7 run-state persistence + resume (Layer 1 control plane, zero-LLM).

Every node-state transition is persisted under
``<state_dir>/<run_id>/`` so a run is fully resumable (spec §2):

::

    <state_dir>/<run_id>/
      tree.json                 # the WorkTree envelope (run_id, root_slug)
      nodes/
        root/node.json          # one file per node, keyed by durable path
        root.0/node.json
        root.1/node.json
        root.1.0/node.json

A node path like ``root.1.0`` maps to the directory ``nodes/root.1.0/`` (the
dots are kept verbatim — they are the durable identity, not path separators), so
the on-disk layout is flat and traversal-free. Resume reads ``tree.json`` + every
``nodes/*/node.json`` back into a :class:`~tree.WorkTree` and hands it to
:func:`~tree.frontier`; completed nodes/stages never re-run.

Pure persistence: deterministic JSON (``sort_keys=True``), no clock. The default
``state_dir`` is ``.foundry/workflow-state``. Each *individual* file is written
in one ``write_text`` call (atomic per file), but :func:`save_tree`'s multi-file
write (envelope + one node.json per node) is **not** atomic across files — a
crash mid-loop can leave a partially-written tree. :func:`load_tree` detects that
(a parent referencing a missing child) and raises a clear ``ValueError`` rather
than letting a downstream ``KeyError`` escape (#990).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from primitives.work_orchestrator.shared.tree import Node, WorkTree

if TYPE_CHECKING:
    from collections.abc import Iterable

#: Default root for run state (relative to the consumer's code root).
DEFAULT_STATE_DIR = Path(".foundry") / "workflow-state"

_TREE_FILENAME = "tree.json"
_NODES_DIRNAME = "nodes"
_NODE_FILENAME = "node.json"

#: A node path is ``root`` optionally followed by ``.<int>`` segments. This is
#: validated before it is ever used as a directory name so a crafted path cannot
#: escape the run directory (defensive — paths are control-plane-minted, but the
#: store is the durable trust boundary).
_NODE_PATH_RE = re.compile(r"^root(?:\.\d+)*$")


def _validate_node_path(path: str) -> None:
    """Raise ``ValueError`` unless *path* is a well-formed node path."""
    if not _NODE_PATH_RE.match(path):
        msg = f"invalid node path {path!r}: must match {_NODE_PATH_RE.pattern}"
        raise ValueError(msg)


def run_dir(state_dir: Path, run_id: str) -> Path:
    """Return the run directory ``<state_dir>/<run_id>`` for *run_id*.

    *run_id* is validated against the same charset as a cache slot (no ``..``,
    no path separators, no leading ``-``) so it cannot redirect the write
    outside *state_dir*.
    """
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", run_id):
        msg = f"invalid run_id {run_id!r}: must match ^[A-Za-z0-9][A-Za-z0-9._-]*$"
        raise ValueError(msg)
    return state_dir / run_id


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """Write *payload* as deterministic JSON in one atomic call."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def _node_dir(state_dir: Path, run_id: str, node_path: str) -> Path:
    """Return ``<run_dir>/nodes/<node_path>`` after validating *node_path*."""
    _validate_node_path(node_path)
    return run_dir(state_dir, run_id) / _NODES_DIRNAME / node_path


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def save_node(tree: WorkTree, node_path: str, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    """Persist a single node's state to ``nodes/<node_path>/node.json``.

    This is the granular sink called after **every** node-state transition so a
    crash mid-run leaves a consistent, resumable tree. Returns the path written.
    """
    node = tree.get(node_path)
    node_file = _node_dir(state_dir, tree.run_id, node_path) / _NODE_FILENAME
    _write_json(node_file, node.model_dump(mode="json"))
    return node_file


def save_envelope(tree: WorkTree, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    """Persist only the run envelope (``tree.json``) — no node bodies (#1004).

    The envelope carries the run identity plus tree-level (non-node) state — the
    opt-in root-level system reviews live on the tree, not on any node (#989), so
    they belong here; dropping them would silently disable the reviews across an
    extract→continue boundary. This is the granular sink for the per-round path,
    where only the envelope + the touched node(s) change — far cheaper than
    rewriting every node file each round (:func:`save_tree` does the full sweep).
    Returns the path written.
    """
    envelope: dict[str, object] = {
        "run_id": tree.run_id,
        "root_slug": tree.root_slug,
        "root_reviews": tree.root_reviews,
    }
    tree_file = run_dir(state_dir, tree.run_id) / _TREE_FILENAME
    _write_json(tree_file, envelope)
    return tree_file


def save_tree(tree: WorkTree, state_dir: Path = DEFAULT_STATE_DIR) -> Path:
    """Persist the whole tree: the envelope plus every node file.

    Writes ``tree.json`` (the envelope, no node bodies) and one
    ``nodes/<path>/node.json`` per node. Use this for the **initial** persist of a
    fresh run; the per-round path persists only the touched nodes via
    :func:`save_envelope` + :func:`save_node` (#1004). Returns the run directory.
    """
    save_envelope(tree, state_dir)
    for node_path in tree.nodes:
        save_node(tree, node_path, state_dir)
    return run_dir(state_dir, tree.run_id)


# ---------------------------------------------------------------------------
# Read / resume path
# ---------------------------------------------------------------------------


def run_exists(run_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> bool:
    """Return ``True`` when a persisted ``tree.json`` exists for *run_id*."""
    return (run_dir(state_dir, run_id) / _TREE_FILENAME).is_file()


def _iter_node_files(rdir: Path) -> Iterable[Path]:
    """Yield every ``nodes/*/node.json`` under *rdir* in sorted order."""
    nodes_root = rdir / _NODES_DIRNAME
    if not nodes_root.is_dir():
        return
    for node_dir in sorted(nodes_root.iterdir()):
        node_file = node_dir / _NODE_FILENAME
        if node_file.is_file():
            yield node_file


def load_tree(run_id: str, state_dir: Path = DEFAULT_STATE_DIR) -> WorkTree:
    """Load a persisted :class:`~tree.WorkTree` for *run_id*.

    Reads the ``tree.json`` envelope and every ``nodes/*/node.json`` back into a
    fully-typed tree. Raises ``FileNotFoundError`` if no run is persisted and
    ``ValueError`` if a state file is malformed or fails validation.
    """
    rdir = run_dir(state_dir, run_id)
    tree_file = rdir / _TREE_FILENAME
    if not tree_file.is_file():
        msg = f"no persisted run {run_id!r} under {state_dir!s} (expected {tree_file!s})"
        raise FileNotFoundError(msg)

    try:
        envelope = json.loads(tree_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"could not read {tree_file!s}: {exc}"
        raise ValueError(msg) from exc

    nodes: dict[str, Node] = {}
    for node_file in _iter_node_files(rdir):
        try:
            raw = json.loads(node_file.read_text(encoding="utf-8"))
            node = Node.model_validate(raw)
        except (OSError, json.JSONDecodeError) as exc:
            msg = f"could not read {node_file!s}: {exc}"
            raise ValueError(msg) from exc
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            msg = f"node state at {node_file!s} failed validation: {exc}"
            raise ValueError(msg) from exc
        nodes[node.path] = node

    # Consistency check (#990): a crash mid-`save_tree` (the multi-file write is
    # not atomic across files) can leave a tree whose parent `children` references
    # a node whose node.json was never written. Surface that as a clear ValueError
    # here rather than a downstream KeyError in frontier()/children_of().
    if "root" not in nodes:
        msg = f"inconsistent run state under {rdir!s}: no root node persisted"
        raise ValueError(msg)
    for node in nodes.values():
        missing = [c for c in node.children if c not in nodes]
        if missing:
            msg = (
                f"inconsistent run state under {rdir!s}: node {node.path!r} references "
                f"child node(s) {missing!r} with no persisted node.json (partial write?)"
            )
            raise ValueError(msg)

    try:
        return WorkTree.model_validate(
            {**envelope, "nodes": {p: n.model_dump() for p, n in nodes.items()}}
        )
    except ValueError as exc:
        msg = f"tree envelope at {tree_file!s} failed validation: {exc}"
        raise ValueError(msg) from exc

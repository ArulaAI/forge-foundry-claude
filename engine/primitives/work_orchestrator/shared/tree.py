"""C7 recursive node tree (Layer 1 control plane, zero-LLM).

A run is a **tree of nodes** (spec ``§2``). Each node owns a spec and a status.
The root node is the GitHub issue; children are work-units the planner carved
out of a parent.

Node-path identity is durable (``root``, ``root.0``, ``root.1.0``, ...). A
``split`` parent stays ``blocked`` until all its children reach a terminal
state, then aggregates their verdicts. A **leaf** is any node the planner calls
``atomic``.

Pure and deterministic: no LLM, no clock, no filesystem. The model is a plain
pydantic structure so it round-trips through :mod:`state_store` byte-for-byte.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

#: A node's lifecycle status (spec §2).
#:
#: * ``pending``   — created, no stage has run yet.
#: * ``running``   — a stage is in flight for this node.
#: * ``blocked``   — a ``split`` parent waiting on its children.
#: * ``clean``     — terminal: the node (and, for a parent, its whole subtree)
#:   passed its node-gate.
#: * ``not-clean`` — terminal: a leaf whose node-gate returned ``review``/``fail``,
#:   or a parent with a non-clean descendant.
#: * ``failed``    — terminal: a hard stage failure halted the node.
#: * ``skipped``   — terminal: the node was skipped (e.g. ``--start-from``).
NodeStatus = Literal[
    "pending",
    "running",
    "blocked",
    "clean",
    "not-clean",
    "failed",
    "skipped",
]

#: The planner's decomposition verdict for a node (C2 §5.2). ``None`` until the
#: plan stage has run.
Decomposition = Literal["atomic", "split"]

#: Statuses that mean the node will not transition further on its own.
_TERMINAL: frozenset[str] = frozenset({"clean", "not-clean", "failed", "skipped"})

#: The root node's durable path.
ROOT_PATH = "root"


def is_terminal(status: NodeStatus) -> bool:
    """Return ``True`` when *status* is a terminal (no-further-work) state."""
    return status in _TERMINAL


# ---------------------------------------------------------------------------
# Node + tree model
# ---------------------------------------------------------------------------


class Node(BaseModel):
    """One node in the recursive work tree.

    ``path`` is the durable identity (``root``, ``root.0``, ``root.1.0``). A node
    is a **leaf** once its planner verdict is ``atomic``; a **split** node has
    children and waits on them. ``completed_stages`` records which model-bearing
    stages have already produced a judgment so a resumed run never re-runs them.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    title: str = ""
    #: Brief spec-seed the parent's split verdict carried for this child; the
    #: ``root`` node seeds from the issue body. C1 expands it into a full spec.
    spec_seed: str = ""
    #: Filesystem path to the node's C1-produced spec, once the spec stage runs.
    spec_path: str | None = None
    status: NodeStatus = "pending"
    decomposition: Decomposition | None = None
    #: Ordered child paths (``root.0``, ``root.1``, ...) when ``decomposition``
    #: is ``split``; empty for a leaf.
    children: list[str] = Field(default_factory=list)
    #: Model-bearing stages that have completed for this node (e.g. ``spec``,
    #: ``plan``). Pure-Python stages are not tracked here.
    completed_stages: list[str] = Field(default_factory=list)
    #: How many times this node's spec has been re-requested after a
    #: ``spec-review`` ``revise`` verdict. Bounded to one retry (spec §2.1).
    spec_revise_count: int = 0
    #: Per-stage verdicts captured from the leaf's ``review`` / ``intent-review``
    #: / ``spec-validate`` rounds. The node-gate folds these DETERMINISTICALLY
    #: (gating.node_gate) into the recommendation — the model supplies only the
    #: gate rationale, not the recommendation (spec §3.1, #987).
    stage_verdicts: dict[str, str] = Field(default_factory=dict)
    #: The node-gate recommendation for a leaf once gating runs.
    gate_verdict: Literal["pass", "review", "fail", "incomplete"] | None = None

    def is_leaf(self) -> bool:
        """A node is a leaf once the planner has judged it ``atomic``."""
        return self.decomposition == "atomic"

    def is_split(self) -> bool:
        """A node is a split once the planner has judged it ``split``."""
        return self.decomposition == "split"

    def is_terminal(self) -> bool:
        """Return ``True`` when this node is in a terminal status."""
        return is_terminal(self.status)


def child_path(parent_path: str, index: int) -> str:
    """Return the durable path of *parent_path*'s *index*-th child.

    ``child_path("root", 0) == "root.0"`` and
    ``child_path("root.1", 2) == "root.1.2"``.
    """
    if index < 0:
        msg = f"child index must be non-negative, got {index}"
        raise ValueError(msg)
    return f"{parent_path}.{index}"


def parent_path(path: str) -> str | None:
    """Return the parent path of *path*, or ``None`` for the root.

    ``parent_path("root.1.0") == "root.1"``; ``parent_path("root") is None``.
    """
    if "." not in path:
        return None
    return path.rsplit(".", 1)[0]


def depth(path: str) -> int:
    """Return the node's depth (``root`` is 0, ``root.0`` is 1, ...)."""
    return path.count(".")


class WorkTree(BaseModel):
    """The whole node tree for one run, keyed by durable node path.

    Holds the resolved root-issue slug (every leaf builds onto
    ``foundry/<root_slug>``) and the flat ``nodes`` map. All traversal helpers
    derive structure from node paths + each node's ``children`` list, so the map
    is the single source of truth and round-trips through :mod:`state_store`.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    #: Slug of the root issue; the single build branch is ``foundry/<root_slug>``.
    root_slug: str
    nodes: dict[str, Node] = Field(default_factory=dict)
    #: Opt-in root-level system reviews (spec §3.2): name -> outcome. A value of
    #: ``None`` means the review is enabled but has not run yet (the orchestrator
    #: emits a model round for it after all leaves settle, #989); a string is its
    #: captured verdict (``clean`` / ``dirty``). Off by default (empty).
    root_reviews: dict[str, str | None] = Field(default_factory=dict)

    # -- construction -------------------------------------------------------

    @classmethod
    def new(
        cls, run_id: str, root_slug: str, root_title: str = "", root_seed: str = ""
    ) -> WorkTree:
        """Create a tree with a single ``pending`` root node."""
        root = Node(path=ROOT_PATH, title=root_title, spec_seed=root_seed)
        return cls(run_id=run_id, root_slug=root_slug, nodes={ROOT_PATH: root})

    # -- accessors ----------------------------------------------------------

    def get(self, path: str) -> Node:
        """Return the node at *path*; raise ``KeyError`` if absent."""
        return self.nodes[path]

    def root(self) -> Node:
        """Return the root node."""
        return self.nodes[ROOT_PATH]

    def children_of(self, path: str) -> list[Node]:
        """Return the child nodes of *path* in deterministic (path) order."""
        node = self.nodes[path]
        return [self.nodes[c] for c in node.children]

    def leaves(self) -> list[Node]:
        """Return every leaf node in deterministic build order.

        Build order is a stable, depth-first pre-order walk by sorted node path
        so all leaves target the single ``foundry/<root_slug>`` branch in one
        deterministic sequence (spec §2: sequential, no intra-run conflicts).
        """
        return [n for n in self._preorder() if n.is_leaf()]

    # -- mutation -----------------------------------------------------------

    def split(self, path: str, children: Iterable[tuple[str, str]]) -> list[Node]:
        """Record a ``split`` verdict on *path*, creating one child per unit.

        *children* is an ordered iterable of ``(title, spec_seed)`` pairs (from
        C2's decomposition verdict). Child nodes are created ``pending`` with
        durable paths ``<path>.0``, ``<path>.1``, ...; the parent becomes
        ``blocked`` and ``split``. Returns the new child nodes in order.
        """
        parent = self.nodes[path]
        new_children: list[Node] = []
        child_paths: list[str] = []
        for index, (title, seed) in enumerate(children):
            cpath = child_path(path, index)
            node = Node(path=cpath, title=title, spec_seed=seed)
            self.nodes[cpath] = node
            new_children.append(node)
            child_paths.append(cpath)
        parent.decomposition = "split"
        parent.children = child_paths
        parent.status = "blocked"
        return new_children

    def mark_atomic(self, path: str) -> Node:
        """Record an ``atomic`` verdict on *path* (it is now a build leaf)."""
        node = self.nodes[path]
        node.decomposition = "atomic"
        node.children = []
        return node

    # -- traversal ----------------------------------------------------------

    def _preorder(self, start: str = ROOT_PATH) -> Iterator[Node]:
        """Yield nodes depth-first pre-order, children in sorted-path order."""
        node = self.nodes[start]
        yield node
        for cpath in sorted(node.children, key=_path_sort_key):
            yield from self._preorder(cpath)

    def walk(self) -> list[Node]:
        """Return all nodes in deterministic depth-first pre-order."""
        return list(self._preorder())


# ---------------------------------------------------------------------------
# Frontier walk
# ---------------------------------------------------------------------------


def _path_sort_key(path: str) -> tuple[int, ...]:
    """Sort node paths by their numeric segments (``root`` sorts first).

    ``root`` -> ``()``; ``root.2`` -> ``(2,)``; ``root.10`` -> ``(10,)`` so
    ``root.2`` precedes ``root.10`` (numeric, not lexicographic).
    """
    segments = path.split(".")
    return tuple(int(s) for s in segments[1:])


def frontier(tree: WorkTree) -> Node | None:
    """Return the next node needing work, or ``None`` when the tree is done.

    The frontier is the first non-terminal node in deterministic build order
    that is **ready** — i.e. not a ``split`` parent still waiting on children.
    A resumed run calls this repeatedly: completed/terminal nodes are skipped,
    so no node or stage is ever re-run.

    Selection order (spec §2 — sequential, deterministic):

    * walk nodes depth-first pre-order by sorted path;
    * skip terminal nodes (``clean``/``not-clean``/``failed``/``skipped``);
    * **never return a ``split`` parent** — a split node has no per-node stage
      (its work *is* its children); it settles only via aggregation. Returning a
      split parent (even one whose children are all terminal but which has not
      yet been aggregated) would hand back a node with no actionable stage and
      could finish the run with the parent still ``blocked`` (#990). Aggregation
      is `aggregate_all`'s job, not the frontier's.
    * the first remaining non-terminal, non-split node is the frontier.
    """
    for node in tree.walk():
        if node.is_terminal():
            continue
        if node.is_split():
            # A split parent never has a per-node stage; skip it regardless of
            # whether its children have settled. It becomes terminal via
            # aggregate_all, after which is_terminal() skips it above.
            continue
        return node
    return None


def _all_children_terminal(tree: WorkTree, node: Node) -> bool:
    """Return ``True`` when every child of *node* is in a terminal status."""
    return all(tree.nodes[c].is_terminal() for c in node.children)


# ---------------------------------------------------------------------------
# Parent/child join (aggregation)
# ---------------------------------------------------------------------------


def aggregate_parent(tree: WorkTree, path: str) -> NodeStatus:
    """Aggregate a ``split`` parent's status from its children and persist it.

    A parent is ``clean`` only when **every** child is ``clean``. Otherwise it is
    ``not-clean`` if any child is ``not-clean``/``failed`` (sibling builds still
    complete so the report stays exhaustive, but the subtree cannot be clean).
    Returns the resolved parent status; raises ``ValueError`` if any child is
    still non-terminal.
    """
    parent = tree.nodes[path]
    if not parent.is_split():
        msg = f"aggregate_parent called on non-split node {path!r}"
        raise ValueError(msg)
    if not _all_children_terminal(tree, parent):
        msg = f"cannot aggregate {path!r}: not all children are terminal"
        raise ValueError(msg)

    statuses = {tree.nodes[c].status for c in parent.children}
    if statuses <= {"clean", "skipped"}:
        resolved: NodeStatus = "clean"
    else:
        resolved = "not-clean"
    parent.status = resolved
    return resolved


def aggregate_all(tree: WorkTree) -> list[str]:
    """Aggregate every ready ``split`` parent, deepest first.

    Walks split parents from the deepest paths up so a grandchild settles before
    its parent, which settles before the grandparent. Idempotent: a parent whose
    children are not all terminal is left untouched.

    Returns the paths of the parents whose status this call actually **changed**
    (deepest first) so a caller can persist only the touched nodes rather than the
    whole tree (#1004). An already-terminal parent that is re-visited but not
    transitioned is not reported.
    """
    splits = [n for n in tree.walk() if n.is_split()]
    # Deepest first: longer paths aggregate before their ancestors.
    splits.sort(key=lambda n: depth(n.path), reverse=True)
    changed: list[str] = []
    for node in splits:
        if _all_children_terminal(tree, node):
            before = node.status
            aggregate_parent(tree, node.path)
            if node.status != before:
                changed.append(node.path)
    return changed


# NOTE (#991): the former ``tree_clean`` helper was removed. It was dead (no
# production caller) and its zero-leaf / skipped reasoning diverged from the live
# "fully clean" predicate. The single source of truth for the PR decision is
# ``gating.should_open_pr`` over ``gating.tree_gate``.

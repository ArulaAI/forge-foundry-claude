"""C7 deterministic WorkflowReport + roll-up renderer (spec §4, §5.1).

Renders the run outcome as Markdown: the node tree, each leaf's node-gate
verdict, the opt-in root-review summary, the overall tree-gate recommendation,
and — when the tree is not fully clean — the resumable-state location so a human
can pick the run back up. The same rendered body is reused as the PR body when a
PR is opened (spec §4).

Pure rendering: no LLM, no clock, no filesystem. Deterministic given a
:class:`~tree.WorkTree` + :class:`~gating.TreeGate`, so it is golden-file
testable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.work_orchestrator.shared.gating import should_open_pr
from primitives.work_orchestrator.shared.tree import depth

if TYPE_CHECKING:
    from primitives.work_orchestrator.shared.gating import TreeGate
    from primitives.work_orchestrator.shared.tree import WorkTree

#: Status -> a compact glyph for the tree rendering.
_STATUS_GLYPH: dict[str, str] = {
    "pending": "○",
    "running": "◐",
    "blocked": "◧",
    "clean": "✅",
    "not-clean": "⚠️",
    "failed": "❌",
    "skipped": "⤼",
}


def render_node_tree(tree: WorkTree) -> str:
    """Render the node tree as an indented Markdown list (deterministic order)."""
    lines = ["## Node tree", ""]
    for node in tree.walk():
        indent = "  " * depth(node.path)
        glyph = _STATUS_GLYPH.get(node.status, "?")
        kind = "split" if node.is_split() else ("leaf" if node.is_leaf() else "node")
        title = f" — {node.title}" if node.title else ""
        lines.append(f"{indent}- {glyph} `{node.path}` ({kind}, {node.status}){title}")
    return "\n".join(lines)


def render_leaf_verdicts(gate: TreeGate) -> str:
    """Render the per-leaf node-gate verdict table (deterministic by path)."""
    lines = ["## Leaf verdicts", "", "| Node | Node-gate |", "|------|-----------|"]
    for path in sorted(gate.leaf_verdicts):
        lines.append(f"| `{path}` | {gate.leaf_verdicts[path]} |")
    if not gate.leaf_verdicts:
        lines.append("| _(no leaves)_ | — |")
    return "\n".join(lines)


def render_root_reviews(gate: TreeGate) -> str:
    """Render the opt-in root-review summary (omitted when none ran)."""
    if not gate.root_reviews:
        return "## Root reviews\n\n_None enabled (system reviews are opt-in, off by default)._"
    lines = ["## Root reviews", "", "| Review | Clean | Summary |", "|--------|-------|---------|"]
    for rr in gate.root_reviews:
        clean = "✅" if rr.clean else "⚠️"
        lines.append(f"| {rr.name} | {clean} | {rr.summary} |")
    return "\n".join(lines)


def render_report(
    tree: WorkTree,
    gate: TreeGate,
    *,
    state_location: str | None = None,
) -> str:
    """Render the full WorkflowReport Markdown (spec §4).

    The header carries the overall tree-gate recommendation and the "fully
    clean" decision. When the tree is **not** fully clean, the resumable
    ``state_location`` is surfaced so a human can resume; when it is clean, the
    report notes a PR may be opened. The body is reused verbatim as the PR body.
    """
    clean = should_open_pr(gate)
    decision = (
        "✅ **Fully clean** — a PR may be opened."
        if clean
        else "⛔ **Not fully clean** — no PR opened. Resume or `--force-pr` for a draft."
    )
    header = [
        f"# Workflow report — `{tree.run_id}`",
        "",
        f"**Build branch:** `foundry/{tree.root_slug}`",
        f"**Tree-gate:** `{gate.recommendation}`",
        f"**Decision:** {decision}",
    ]
    if gate.rationale:
        header.append(f"**Rationale:** {gate.rationale}")
    if not clean and state_location:
        header.append(f"**Resume from:** `{state_location}`")
    sections = [
        "\n".join(header),
        render_node_tree(tree),
        render_leaf_verdicts(gate),
        render_root_reviews(gate),
    ]
    return "\n\n".join(sections) + "\n"

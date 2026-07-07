"""C7 two-tier gating: node-gate (per leaf) + tree-gate (root) (spec §4).

Two gate tiers compute the ``pass | review | fail | incomplete`` recommendation
that decides whether a PR is opened:

* **Node-gate (per leaf):** C6 ``/confidence-gate`` over the leaf's review chain
  + intent-review + spec-validate reports. A non-``pass`` leaf marks the node
  ``not-clean``; siblings still complete so the report stays exhaustive, but the
  tree cannot be clean.
* **Tree-gate (root, final):** C6 over **all** node verdicts **plus** the opt-in
  root-level system reviews (S70/S60/S10/C8 — off by default). This is the
  overall recommendation.

The **"fully clean" condition** (spec §4): a PR is opened **only if the tree-gate
is ``pass``** — every leaf node-gate is ``pass`` *and* every enabled root review
is clean. :func:`should_open_pr` is the single predicate the PR-prep layer asks.

This module owns the **aggregation logic** over already-computed verdicts; the
per-leaf deterministic risk-scoring is C6's job (the node-gate stage's judgment,
applied in :mod:`judgments`). Pure and deterministic: no LLM, no clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from primitives.work_orchestrator.shared.tree import is_terminal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from primitives.work_orchestrator.shared.tree import WorkTree

#: The C7 gate recommendation. Extends C6's ``pass|review|fail`` with
#: ``incomplete`` for the no-inputs case (spec §3.1 node-gate, §4 tree-gate).
GateRecommendation = Literal["pass", "review", "fail", "incomplete"]

#: Severity rank for folding many verdicts into one (worst wins, but a present
#: ``review``/``fail`` always beats ``incomplete``, which beats ``pass``).
_RANK: dict[str, int] = {"pass": 0, "incomplete": 1, "review": 2, "fail": 3}
_BY_RANK: dict[int, GateRecommendation] = {
    0: "pass",
    1: "incomplete",
    2: "review",
    3: "fail",
}


class RootReview(BaseModel):
    """One opt-in root-level system review's outcome (spec §3.2).

    ``clean`` is ``True`` when the review found nothing gate-blocking.
    ``S70``/``S60``/``S10``/``C8`` map to compliance/data-flow/threat/arch.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    clean: bool
    summary: str = ""


class TreeGate(BaseModel):
    """The final tree-gate result feeding the PR decision (spec §4)."""

    model_config = ConfigDict(frozen=True)

    recommendation: GateRecommendation
    #: Per-leaf node-gate verdicts, keyed by node path.
    leaf_verdicts: dict[str, GateRecommendation] = Field(default_factory=dict)
    #: Opt-in root-level reviews that ran (empty when none enabled).
    root_reviews: list[RootReview] = Field(default_factory=list)
    #: ``True`` when **any** node in the tree halted with a hard ``failed`` status
    #: (a build / spec-review failure), regardless of whether it is an atomic leaf.
    #: Drives the CLI's exit-5 mapping — a spec-review failure halts a node *before*
    #: it is ever classified as a leaf, so it would not otherwise surface in
    #: ``leaf_verdicts`` (#986).
    has_hard_failure: bool = False
    rationale: str = ""


def fold(recommendations: Sequence[GateRecommendation]) -> GateRecommendation:
    """Fold many recommendations into one — worst-rank wins; empty ⟹ ``incomplete``.

    Ordering (spec §3.1/§4): ``fail`` > ``review`` > ``incomplete`` > ``pass``.
    An empty input means nothing was gated yet, so the fold is ``incomplete``.
    """
    if not recommendations:
        return "incomplete"
    worst = max(_RANK[r] for r in recommendations)
    return _BY_RANK[worst]


def node_gate(
    *,
    review: GateRecommendation,
    intent_review: GateRecommendation,
    spec_validate: GateRecommendation,
) -> GateRecommendation:
    """Compute a leaf's node-gate from its three upstream verdicts (spec §3.1, §4).

    The leaf's review chain, the clean-room intent-review, and spec-validate each
    contribute a ``pass|review|fail|incomplete``; the node-gate is their fold
    (worst wins). C6's deterministic risk-scoring produces each input verdict;
    this is the deterministic combination the control plane applies.
    """
    return fold([review, intent_review, spec_validate])


def tree_gate(
    tree: WorkTree,
    *,
    root_reviews: Sequence[RootReview] = (),
    rationale: str = "",
) -> TreeGate:
    """Compute the final tree-gate over all leaf verdicts + opt-in root reviews.

    The recommendation is the fold of every leaf's recorded ``gate_verdict``
    **plus** a ``fail`` contribution from any non-clean enabled root review
    **plus** a ``fail`` contribution from any node anywhere in the tree that
    halted with a hard ``failed`` status (a spec-review / build failure halts a
    node *before* decomposition, so it is never an atomic leaf and would not
    otherwise surface — #986). A leaf that is terminal but has no ``gate_verdict``
    (e.g. ``skipped``) contributes per :func:`_leaf_contribution`.
    """
    leaves = tree.leaves()
    leaf_verdicts: dict[str, GateRecommendation] = {}
    contributions: list[GateRecommendation] = []
    for leaf in leaves:
        verdict = _leaf_contribution(leaf.gate_verdict, leaf.status)
        leaf_verdicts[leaf.path] = verdict
        contributions.append(verdict)

    for rr in root_reviews:
        contributions.append("pass" if rr.clean else "fail")

    # A hard failure on ANY node (not just a leaf) is a tree-gate fail. This is
    # what makes a halted-at-spec-review node reach exit 5 (#986).
    has_hard_failure = any(node.status == "failed" for node in tree.walk())
    if has_hard_failure:
        contributions.append("fail")

    # All-skipped tree (#991): a `skipped` leaf contributes `pass` so a single
    # deliberately-skipped sibling doesn't block a clean tree — but a tree where
    # EVERY leaf was skipped means nothing was actually built or gated, which must
    # not read as "fully clean → open a PR". Force `incomplete` in that case.
    if leaves and all(leaf.status == "skipped" for leaf in leaves):
        contributions.append("incomplete")

    return TreeGate(
        recommendation=fold(contributions),
        leaf_verdicts=leaf_verdicts,
        root_reviews=list(root_reviews),
        has_hard_failure=has_hard_failure,
        rationale=rationale,
    )


def _leaf_contribution(
    gate_verdict: GateRecommendation | None,
    status: str,
) -> GateRecommendation:
    """Map a leaf's (gate_verdict, status) to its tree-gate contribution.

    A gated leaf contributes its ``gate_verdict`` directly. An ungated leaf
    contributes ``pass`` if ``skipped`` (intentionally excluded), ``fail`` if it
    ``failed`` a hard stage, else ``incomplete`` (not yet gated).
    """
    if gate_verdict is not None:
        return gate_verdict
    if status == "skipped":
        return "pass"
    if status == "failed":
        return "fail"
    return "incomplete"


def should_open_pr(gate: TreeGate) -> bool:
    """Return ``True`` iff the tree is *fully clean* — a PR may be opened (spec §4).

    A PR is opened **only** when the tree-gate recommendation is ``pass`` — i.e.
    every leaf node-gate passed *and* every enabled root review was clean. Any
    ``review``/``fail``/``incomplete`` means no PR (unless ``--force-pr`` opens a
    draft, which is the PR layer's concern, not this predicate's).
    """
    return gate.recommendation == "pass"


def all_terminal(tree: WorkTree) -> bool:
    """Return ``True`` when every node in the tree is in a terminal status.

    The tree-gate is meaningful only once the whole tree has settled; the CLI
    control loop calls this before computing the final gate.
    """
    return all(is_terminal(n.status) for n in tree.walk())

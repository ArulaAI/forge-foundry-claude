"""C7 orchestrator-level extract/continue driver (spec §3, §5.3).

Ties the control plane together into the two CLI legs:

* **extract** (``--extract-only`` or the start of a fresh run): walk the tree to
  the next frontier node + stage needing model work, write ``round-N-prompts.json``,
  persist, and signal *awaiting judgments* (exit 7). When the tree has settled,
  compute the tree-gate and render the final report instead.
* **continue** (``--continue --judgments <path>``): load the run, apply the one
  round's judgment (advancing node/stage state + the split-vs-build decision),
  aggregate ready split parents, persist, then either emit the next round
  (exit 7) or — when the tree is complete — render the report.

The CLI is a thin Click wrapper over :func:`extract_leg` / :func:`continue_leg`;
this module owns the deterministic loop. No LLM, no clock. The model only ever
sees one compact structured prompt per round.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from primitives._shared.protocol import write_prompts
from primitives.work_orchestrator.shared import (
    gating,
    judgments,
    pr,
    prompts,
    report,
    state_store,
)
from primitives.work_orchestrator.shared.stages import next_stage
from primitives.work_orchestrator.shared.tree import WorkTree, aggregate_all, frontier

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._shared.protocol import JudgmentEnvelope, PromptEnvelope
    from primitives.work_orchestrator.shared.gating import RootReview

#: The exit-7 marker (spec §5.3): a round-N-prompts.json was written; the harness
#: must judge it and re-invoke with --continue.
AWAITING_JUDGMENTS = 7

_PROMPTS_FILENAME = "round-{n}-prompts.json"


@dataclass(frozen=True)
class RunOptions:
    """Side-effect options for the Mode-1 stages (issue / branch / pr) (#985).

    The control plane runs ``issue`` (gh issue view), ``branch`` (git checkout),
    and ``pr`` (gh pr create) as deterministic Mode-1 stages — they touch the
    outside world. ``run_side_effects=False`` keeps every gh/git call out (the
    default for unit tests and the acceptance harness); the CLI sets it True.
    """

    project_root: Path
    no_branch: bool = False
    no_pr: bool = False
    force_pr: bool = False
    push: bool = False
    pr_title: str = ""
    run_side_effects: bool = False


@dataclass(frozen=True)
class LegResult:
    """Outcome of one extract/continue leg.

    ``awaiting`` is ``True`` when a prompts file was written (exit 7). When the
    tree is complete, ``awaiting`` is ``False`` and ``report_md`` + ``gate`` hold
    the final roll-up; ``prompts_path`` names the written prompts file (or None).
    ``pr_outcome`` carries the result of the ``pr`` Mode-1 stage when the tree
    finished clean (or ``--force-pr``); ``None`` while awaiting or when no PR was
    attempted.
    """

    awaiting: bool
    round_no: int
    prompts_path: Path | None = None
    report_md: str | None = None
    gate: gating.TreeGate | None = None
    pr_outcome: pr.PrOutcome | None = None
    branch_reason: str | None = None
    #: True when the ``branch`` Mode-1 stage failed and the run was halted before
    #: emitting any prompt (#1006) — the CLI maps this to a hard exit (2). Never
    #: write prompts against the wrong branch.
    branch_failed: bool = False


def _round_no_of(path: Path) -> int:
    """Parse the round number out of a ``round-<N>-prompts.json`` filename.

    Returns 0 for a name that doesn't match (so it can't raise the max).
    """
    m = re.match(r"round-(\d+)-prompts\.json$", path.name)
    return int(m.group(1)) if m else 0


def _next_round_no(tree: WorkTree, state_dir: Path) -> int:
    """Return the next round number — one past the **highest** existing prompts file.

    Uses ``max(parsed round numbers)+1``, NOT ``count+1`` (#990): a gap in the
    ``round-{1..N}`` sequence (e.g. a crash between writing a prompt and the next
    persist, or manual cleanup) must never cause the next write to collide with
    and overwrite an existing round file.
    """
    rdir = state_store.run_dir(state_dir, tree.run_id)
    if not rdir.is_dir():
        return 1
    rounds = [_round_no_of(p) for p in rdir.glob("round-*-prompts.json")]
    return (max(rounds) + 1) if rounds else 1


def _persist_round(tree: WorkTree, state_dir: Path, changed_nodes: list[str]) -> None:
    """Persist the envelope + the nodes this round actually changed (#1004).

    The per-round sink: instead of rewriting every node file each round
    (``save_tree``), write only ``tree.json`` plus the node files for
    *changed_nodes* (the parents :func:`aggregate_all` just transitioned). The
    judged node itself is persisted by ``continue_leg`` via ``save_node`` before
    this runs, so it is not repeated here. Reduces per-round writes from O(n) to
    O(1 + aggregated).
    """
    state_store.save_envelope(tree, state_dir)
    for path in changed_nodes:
        state_store.save_node(tree, path, state_dir)


def _write_round_prompts(
    tree: WorkTree, state_dir: Path, envelope: PromptEnvelope, round_no: int
) -> Path:
    """Write ``round-<N>-prompts.json`` and return its path (#1004 dedupe)."""
    rdir = state_store.run_dir(state_dir, tree.run_id)
    prompts_path = rdir / _PROMPTS_FILENAME.format(n=round_no)
    write_prompts(envelope, prompts_path)
    return prompts_path


def _emit_next_or_finish(
    tree: WorkTree,
    state_dir: Path,
    *,
    root_reviews: tuple[RootReview, ...],
    options: RunOptions | None = None,
) -> LegResult:
    """Emit the next round's prompt, or finish with the tree-gate + report + PR.

    Aggregates ready split parents first (so a parent settles once its children
    do), then asks the frontier for the next node+stage. A frontier node with a
    pending model stage → write a prompt (exit 7). No frontier (or only Mode-1
    work left) with a fully-settled tree → compute the tree-gate, render the
    report, and run the ``pr`` Mode-1 stage (open a PR iff fully clean, or a draft
    on ``--force-pr``; never if ``--no-pr``) (#985).
    """
    # Aggregation is the only node-state mutation in this function; persist just
    # the parents it transitioned (plus the envelope) rather than rewriting every
    # node file each round (#1004). The frontier node is not mutated by emitting a
    # prompt — its state changed (if at all) in the prior continue_leg.apply_round,
    # which already persisted it via save_node.
    aggregated = aggregate_all(tree)
    _persist_round(tree, state_dir, aggregated)

    node = frontier(tree)
    if node is not None:
        stage = next_stage(node)
        if stage is not None:
            round_no = _next_round_no(tree, state_dir)
            envelope = prompts.build_prompt(tree, node, stage, round_no)
            prompts_path = _write_round_prompts(tree, state_dir, envelope, round_no)
            return LegResult(awaiting=True, round_no=round_no, prompts_path=prompts_path)

    # All leaves have settled. If any opt-in root-level system review (spec §3.2)
    # is enabled-but-unrun, emit them ALL in ONE batch round — they run ONCE at the
    # root against the assembled tree, are mutually independent, and feed the final
    # tree-gate (#989). Batching avoids one orchestration round-trip per review
    # (#1007). Emitted in the canonical build-order via prompts.order_root_reviews,
    # NOT the dict's key order — the state envelope is persisted with sort_keys=True,
    # so tree.root_reviews would otherwise come back alphabetized after a resume.
    pending_reviews = prompts.order_root_reviews(
        [n for n, v in tree.root_reviews.items() if v is None]
    )
    if pending_reviews:
        round_no = _next_round_no(tree, state_dir)
        envelope = prompts.build_root_review_batch_prompt(tree, pending_reviews, round_no)
        prompts_path = _write_round_prompts(tree, state_dir, envelope, round_no)
        return LegResult(awaiting=True, round_no=round_no, prompts_path=prompts_path)

    # No model work remains — settle, gate, render, and run the pr Mode-1 stage.
    state_location = str(state_store.run_dir(state_dir, tree.run_id))
    # Fold the CAPTURED root-review verdicts (run as real rounds, #989) — falling
    # back to any placeholders the caller supplied (e.g. a dry run).
    effective_reviews = _resolved_root_reviews(tree) or root_reviews
    gate = gating.tree_gate(tree, root_reviews=effective_reviews)
    report_md = report.render_report(tree, gate, state_location=state_location)
    pr_outcome = _run_pr_stage(tree, gate, report_md, options)
    return LegResult(
        awaiting=False,
        round_no=_next_round_no(tree, state_dir) - 1,
        report_md=report_md,
        gate=gate,
        pr_outcome=pr_outcome,
    )


def _resolved_root_reviews(tree: WorkTree) -> tuple[RootReview, ...]:
    """Build RootReview objects from the tree's captured root-review verdicts (#989).

    Only reviews that have actually run (verdict not ``None``) are included; their
    ``clean`` flag is set from the captured ``clean``/``dirty`` verdict. Returns
    an empty tuple when no root review ran (the caller may then fall back to any
    supplied placeholders).
    """
    return tuple(
        gating.RootReview(name=name, clean=(verdict == "clean"))
        for name, verdict in sorted(tree.root_reviews.items())
        if verdict is not None
    )


def _run_pr_stage(
    tree: WorkTree,
    gate: gating.TreeGate,
    report_md: str,
    options: RunOptions | None,
) -> pr.PrOutcome | None:
    """Run the ``pr`` Mode-1 stage (spec §4) — open one PR iff the tree is clean.

    Returns ``None`` when no PR was attempted: no options provided (pure-sequencer
    callers / tests), ``--no-pr`` set, or the tree is not clean and ``--force-pr``
    is off. Otherwise delegates to :func:`pr.prepare_pr` (which itself enforces
    the "only if clean, else draft on force" rule) and attaches the report via
    ``gh pr comment``. No push by default (``--push`` pushes the feature branch
    only via :func:`pr.prepare_pr`); never pushes/merges the base branch, never
    auto-merges (#1107).
    """
    if options is None or options.no_pr:
        return None
    title = options.pr_title or f"feat: {tree.root_slug}"
    outcome = pr.prepare_pr(
        gate=gate,
        root_slug=tree.root_slug,
        title=title,
        body=report_md,
        project_root=options.project_root,
        force_pr=options.force_pr,
        push=options.push,
        run_gh=options.run_side_effects,
    )
    if outcome.opened and outcome.url and options.run_side_effects:
        pr.comment_report(
            pr_ref=outcome.url,
            body=report_md,
            project_root=options.project_root,
            run_gh=options.run_side_effects,
        )
    return outcome


def extract_leg(
    tree: WorkTree,
    state_dir: Path,
    *,
    root_reviews: tuple[RootReview, ...] = (),
    options: RunOptions | None = None,
) -> LegResult:
    """Start (or resume the head of) a run: branch, persist the tree, emit round 1.

    Runs the ``branch`` Mode-1 stage first (create + check out
    ``foundry/<root-slug>``, unless ``--no-branch``; refuses a dirty tree, never
    pushes — #985), persists the fresh tree, then drives :func:`_emit_next_or_finish`
    once — for a new run this writes ``round-1-prompts.json`` for the root's
    ``spec`` stage.
    """
    branch_reason: str | None = None
    if options is not None and not options.no_branch and options.run_side_effects:
        outcome = pr.ensure_branch(tree.root_slug, options.project_root, run_git=True)
        branch_reason = outcome.reason or None
        if not outcome.opened:
            # The branch stage failed (dirty tree, or git could not create/check
            # out the build branch). HALT before emitting any prompt — running a
            # round against the wrong branch would corrupt the build (#1006).
            return LegResult(
                awaiting=False, round_no=0, branch_failed=True, branch_reason=branch_reason
            )
    state_store.save_tree(tree, state_dir)
    result = _emit_next_or_finish(tree, state_dir, root_reviews=root_reviews, options=options)
    if branch_reason is not None:
        result = replace(result, branch_reason=branch_reason)
    return result


def resume_leg(
    tree: WorkTree,
    state_dir: Path,
    *,
    root_reviews: tuple[RootReview, ...] = (),
    options: RunOptions | None = None,
) -> LegResult:
    """Resume an already-persisted run without re-seeding or re-branching (#1194).

    A bare (non-``--continue``) re-invocation of an existing run must **resume**
    it, not clobber it with a fresh tree from the ``spec`` stage (that is the
    contract in ``_mint_run_id``: same issue ref → same run dir → resume). This
    re-drives the frontier on the *loaded* tree:

    * model work still pending → emit the next round's prompt (exit 7);
    * the tree has already **settled** → run the finalize + ``pr`` Mode-1 stage,
      honoring ``--force-pr`` / ``--push`` — so ``--force-pr`` on a finalized
      ``review`` run jumps straight to opening the (draft) PR (3a) and ``--push``
      resume runs the push/PR stage instead of crashing in the ``--continue``
      replay guard (3b).

    Unlike :func:`extract_leg` it does **not** run the ``branch`` Mode-1 stage or
    ``save_tree`` a fresh tree — the branch and the run already exist on disk.
    """
    return _emit_next_or_finish(tree, state_dir, root_reviews=root_reviews, options=options)


def continue_leg(
    run_id: str,
    judgment_envelope: JudgmentEnvelope,
    state_dir: Path,
    *,
    root_reviews: tuple[RootReview, ...] = (),
    options: RunOptions | None = None,
) -> LegResult:
    """Apply one round's judgment, then emit the next round or finish.

    Loads the persisted tree, applies the single-item judgment (advancing
    node/stage state + the split-vs-build decision + spec-review retry bound),
    persists the touched node, then re-drives the frontier. On the terminal round
    the finish path runs the ``pr`` Mode-1 stage (#985) per *options*.
    """
    tree = state_store.load_tree(run_id, state_dir)
    node = judgments.apply_round(tree, judgment_envelope)
    # Persist the judged node AND any nodes its judgment created — a `split` plan
    # judgment calls tree.split(), which mints new child nodes that must hit disk
    # (a root-review judgment touches the root only). Persisting node.children
    # covers the split case without rewriting the whole tree (#1004).
    state_store.save_node(tree, node.path, state_dir)
    for child_path in node.children:
        state_store.save_node(tree, child_path, state_dir)
    return _emit_next_or_finish(tree, state_dir, root_reviews=root_reviews, options=options)

"""``forge-validate work-orchestrator`` — the C7 extract/continue CLI (#971).

The Python-load-bearing driver for C7 ``/work-orchestrator`` (epic #966, spec
§3/§5.3/§5.4). A **multi-round** extract/continue loop over the recursive node
tree, driven deterministically by the control plane in
``primitives/work_orchestrator/shared/``. The model only ever produces one
compact structured artifact per round.

Legs:

* **extract** (default / ``--extract-only``): walk the tree to the next frontier
  node + stage, write ``round-N-prompts.json`` under ``--state-dir/<run-id>``,
  exit 7. When the tree has settled, render the final report instead.
* **continue** (``--continue --judgments <path>``): apply the round's judgment,
  advance state, then emit the next round (exit 7) or render the report.

Exit codes (spec §5.3):

* ``0`` — tree complete, tree-gate ``pass`` (PR opened, or prepared if ``--no-pr``)
* ``2`` — issue ref not found / unresolvable, or ``--continue`` can't find the run
* ``3`` — ``--workflow-yaml`` or judgments envelope malformed
* ``4`` — tree-gate not clean (``review`` / ``fail`` / ``incomplete``) — no PR
  opened, report surfaced. ``incomplete`` means the tree never finished gating
  (a leaf terminal-but-ungated, zero leaves, or an all-skipped tree, #991); it
  is *not clean* and so shares exit 4, but the surfaced report names it distinctly
  from a reviewed ``review``/``fail`` so a caller can tell "needs a human" from
  "never completed".
* ``5`` — a hard stage failure halted a node (build / spec-review failure)
* ``7`` — awaiting judgments (a ``round-N-prompts.json`` was written)
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from primitives.work_orchestrator.shared.orchestrator import LegResult
    from primitives.work_orchestrator.shared.tree import WorkTree

# Default state directory matches the M-tier convention (.foundry/workflow-state).
_DEFAULT_STATE_DIR = ".foundry/workflow-state"

#: Hermetic-test seam (#985): when set truthy, the Mode-1 stages (issue / branch /
#: pr) skip every gh/git side effect — the issue is not fetched, no branch is
#: created, and no PR is opened. The control-plane judgment loop, gating, and the
#: report still run end-to-end. Mirrors the repo's ``CI`` / ``FORGE_FOUNDRY_CACHE_BASE``
#: hermetic-test conventions; never needed in real use.
_OFFLINE_ENV = "FORGE_WORK_ORCHESTRATOR_OFFLINE"


def _offline() -> bool:
    return os.environ.get(_OFFLINE_ENV, "").lower() in ("1", "true", "yes")


#: SEC-8: a GitHub issue ref — a URL, ``owner/repo#N``, or bare ``#N``/``N``. The
#: first char excludes ``-`` so the value can never be parsed as a flag by any
#: downstream ``gh`` argv. The body admits only the issue-ref alphabet.
_VALID_ISSUE_REF = re.compile(r"^[A-Za-z0-9#][A-Za-z0-9._/:#-]*$")

#: A run id / start-from node path: same charset as the state store enforces.
_VALID_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _slugify(issue_ref: str) -> str:
    """Derive a deterministic, branch-safe root slug from *issue_ref*.

    The build branch is ``foundry/<slug>``; the slug is the issue ref reduced to
    ``[a-z0-9-]`` (other chars → ``-``, collapsed, trimmed), so it is stable and
    structurally safe for ``git``/``gh`` argv.
    """
    lowered = issue_ref.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "issue"


def _mint_run_id(slug: str) -> str:
    """Mint a deterministic run id from *slug* (no clock — resumable by construction).

    The run id is ``run-<slug>``; passing the same ``<issue-ref>`` resolves to the
    same run dir, so a re-invocation without ``--run-id`` resumes rather than
    forking a new run.
    """
    return f"run-{slug}"


@click.command("work-orchestrator")
@click.argument("issue_ref")
@click.option("--code-root", "code_root", default=".", help="Source tree root (default: cwd).")
@click.option(
    "--state-dir",
    "state_dir",
    default=_DEFAULT_STATE_DIR,
    help="Root for run state; the run dir is <state-dir>/<run-id>.",
)
@click.option(
    "--run-id",
    "run_id",
    default=None,
    help="Run subdir name (default: derived from the issue ref).",
)
@click.option(
    "--extract-only",
    "extract_only",
    is_flag=True,
    help="Write the next round's prompts and exit 7.",
)
@click.option("--continue", "continue_", is_flag=True, help="Resume a run; requires --judgments.")
@click.option(
    "--judgments",
    "judgments_path",
    default=None,
    help="Harness-written round-N-judgments.json (with --continue).",
)
@click.option(
    "--start-from", "start_from", default=None, help="Resume cursor: a node path to restart from."
)
@click.option("--no-branch", "no_branch", is_flag=True, help="Skip the branch stage.")
@click.option("--no-pr", "no_pr", is_flag=True, help="Prepare but do not open a PR.")
@click.option(
    "--force-pr", "force_pr", is_flag=True, help="Open a draft PR even when the tree is not clean."
)
@click.option(
    "--push",
    "push",
    is_flag=True,
    help="Push the feature branch (foundry/<slug>) to the remote before opening the PR. "
    "Off by default (C7 does not push); without it, the exact push + create commands are "
    "surfaced when the branch is not already on the remote.",
)
@click.option(
    "--with-compliance", "with_compliance", is_flag=True, help="Run S70 compliance-map at the root."
)
@click.option(
    "--with-threat-model",
    "with_threat_model",
    is_flag=True,
    help="Run S10 threat-model at the root.",
)
@click.option(
    "--with-data-flow", "with_data_flow", is_flag=True, help="Run S60 data-flow-map at the root."
)
@click.option(
    "--with-arch-review",
    "with_arch_review",
    is_flag=True,
    help="Run C8 architecture-review at the root.",
)
@click.option(
    "--workflow-yaml",
    "workflow_yaml",
    default=None,
    help=(
        "Optional workflow override YAML (accepted and validated; contents are "
        "not yet consumed — reserved for future use). Malformed/oversized → exit 3."
    ),
)
@click.option(
    "--output", "output", default=None, help="Write the final report to this path (else stdout)."
)
def cli(  # noqa: PLR0913 — the documented CLI surface (spec §5.4)
    issue_ref: str,
    code_root: str,
    state_dir: str,
    run_id: str | None,
    extract_only: bool,
    continue_: bool,
    judgments_path: str | None,
    start_from: str | None,
    no_branch: bool,
    no_pr: bool,
    force_pr: bool,
    push: bool,
    with_compliance: bool,
    with_threat_model: bool,
    with_data_flow: bool,
    with_arch_review: bool,
    workflow_yaml: str | None,
    output: str | None,
) -> None:
    """Drive a GitHub issue to a reviewable PR via the recursive node tree.

    Default / ``--extract-only`` writes the next round's prompts and exits 7.
    ``--continue --judgments <path>`` applies the round and emits the next one
    (exit 7) or renders the report (0/4/5). See the module docstring for exit codes.
    """
    # --- Flag-combo validation --------------------------------------------
    if extract_only and continue_:
        raise click.UsageError("--extract-only and --continue are mutually exclusive.")
    if continue_ and judgments_path is None:
        raise click.UsageError("--continue requires --judgments <path>.")

    # --- SEC-8: validate the issue ref (leading '-' rejected) -------------
    if not _VALID_ISSUE_REF.match(issue_ref):
        click.echo(f"Error: invalid issue ref {issue_ref!r}", err=True)
        sys.exit(2)
    if run_id is not None and not _VALID_RUN_ID.match(run_id):
        click.echo(f"Error: invalid --run-id {run_id!r}", err=True)
        sys.exit(2)
    if start_from is not None and not _VALID_RUN_ID.match(start_from.replace(".", "_")):
        # node paths contain dots; validate the dot-free form for the charset.
        click.echo(f"Error: invalid --start-from {start_from!r}", err=True)
        sys.exit(2)

    # --- Lazy imports (cheap cold-start). ---------------------------------
    from dataclasses import replace  # noqa: PLC0415

    from primitives._shared.protocol import (  # noqa: PLC0415
        MalformedJudgmentsError,
        read_judgments,
    )
    from primitives.work_orchestrator.shared import (  # noqa: PLC0415
        orchestrator,
        pr,
        state_store,
    )
    from primitives.work_orchestrator.shared.tree import WorkTree  # noqa: PLC0415

    resolved_state = Path(state_dir)
    slug = _slugify(issue_ref)
    resolved_run_id = run_id or _mint_run_id(slug)

    # --code-root must be an existing directory (#1010): it becomes the gh/git cwd,
    # so a bad path otherwise surfaces deep inside a Mode-1 stage as the misleading
    # "issue ref not found" exit-2. Catch it here with a specific message.
    if not Path(code_root).is_dir():
        click.echo(f"Error: --code-root not found or not a directory: {code_root!r}", err=True)
        sys.exit(2)

    # --workflow-yaml: parse it if given; malformed → exit 3 (spec §5.3, #989).
    if workflow_yaml is not None and not _parse_workflow_yaml(workflow_yaml):
        click.echo(f"Error: --workflow-yaml {workflow_yaml!r} is missing or malformed", err=True)
        sys.exit(3)

    # Enabled opt-in root-level system reviews (spec §3.2). These now RUN as real
    # rounds at the root (#989) — seeded onto the tree as pending (verdict None).
    enabled_review_names = _enabled_root_review_names(
        with_compliance=with_compliance,
        with_threat_model=with_threat_model,
        with_data_flow=with_data_flow,
        with_arch_review=with_arch_review,
    )

    # Mode-1 side-effect options (issue / branch / pr) — these touch gh/git, so
    # the CLI runs them for real (#985), unless the hermetic-test seam is set.
    run_side_effects = not _offline()
    options = orchestrator.RunOptions(
        project_root=Path(code_root),
        no_branch=no_branch,
        no_pr=no_pr,
        force_pr=force_pr,
        push=push,
        pr_title=f"feat: {issue_ref}",
        run_side_effects=run_side_effects,
    )

    try:
        if continue_:
            assert judgments_path is not None  # guaranteed by validation
            try:
                envelope = read_judgments(Path(judgments_path))
            except MalformedJudgmentsError as exc:
                click.echo(f"Error: {exc}", err=True)
                sys.exit(3)
            result = orchestrator.continue_leg(
                resolved_run_id,
                envelope,
                resolved_state,
                options=options,
            )
        elif start_from is not None:
            # Resume an already-decomposed run at NODE (#1001). The tree only has
            # child nodes once `plan` has run in a prior continue leg, so the
            # cursor MUST operate on the loaded tree — a fresh `WorkTree.new` has
            # only the root and `--start-from root.N` would be a silent no-op.
            if not state_store.run_exists(resolved_run_id, resolved_state):
                click.echo(
                    f"Error: --start-from {start_from!r} needs an existing run; "
                    f"no persisted run {resolved_run_id!r} under {resolved_state!s} "
                    "(start a run first, then resume with --run-id ... --start-from)",
                    err=True,
                )
                sys.exit(2)
            tree = _load_tree_or_exit(resolved_run_id, resolved_state)
            try:
                _apply_start_from(tree, start_from)
            except KeyError as exc:
                click.echo(f"Error: {exc.args[0]}", err=True)
                sys.exit(2)
            # The branch already exists from the original run — don't re-create it.
            resume_options = replace(options, no_branch=True)
            result = orchestrator.extract_leg(tree, resolved_state, options=resume_options)
        elif state_store.run_exists(resolved_run_id, resolved_state):
            # A bare (non-`--continue`) re-invocation of an EXISTING run must
            # RESUME it, not clobber it with a fresh tree from the `spec` stage
            # (#1194 3a): that is the resumable-by-construction contract in
            # `_mint_run_id` — same issue ref → same run dir → resume. Load the
            # persisted tree and re-drive the frontier: emit the next round if
            # work remains, or run the finalize + `pr` Mode-1 stage (honoring
            # `--force-pr` / `--push`) if the tree has already settled. This is
            # also the correct way to (re)open/push the PR on a terminal run,
            # replacing the `--continue`-on-settled path that raised the confusing
            # `frontier is None` replay-guard error (#1194 3b). The branch and the
            # seeded root reviews already exist on disk — do not re-create them.
            tree = _load_tree_or_exit(resolved_run_id, resolved_state)
            resume_options = replace(options, no_branch=True)
            result = orchestrator.resume_leg(tree, resolved_state, options=resume_options)
        else:
            # `issue` Mode-1 stage: fetch the real title + body to seed the root
            # node, so the root spec stage no longer runs against the raw ref (#985).
            details = pr.fetch_issue(issue_ref, options.project_root, run_gh=run_side_effects)
            if run_side_effects and details is None:
                click.echo(f"Error: issue ref {issue_ref!r} not found / unresolvable", err=True)
                sys.exit(2)
            tree = WorkTree.new(
                resolved_run_id,
                slug,
                root_title=(details.title if details else "") or issue_ref,
                root_seed=details.body if details else "",
            )
            # Seed enabled root reviews as pending; they run after the leaves (#989).
            tree.root_reviews = dict.fromkeys(enabled_review_names)
            result = orchestrator.extract_leg(tree, resolved_state, options=options)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    _finish(result, output=output)


def _enabled_root_review_names(
    *,
    with_compliance: bool,
    with_threat_model: bool,
    with_data_flow: bool,
    with_arch_review: bool,
) -> list[str]:
    """Return the enabled opt-in root-review names (off by default, spec §3.2, #989).

    These now RUN as real model rounds at the root (seeded onto the tree as
    pending); the tree-gate folds the captured verdicts. Disabled reviews
    contribute nothing — the default run gates on leaf verdicts alone.
    """
    return [
        name
        for flag, name in (
            (with_compliance, "S70 compliance-map"),
            (with_threat_model, "S10 threat-model"),
            (with_data_flow, "S60 data-flow-map"),
            (with_arch_review, "C8 architecture-review"),
        )
        if flag
    ]


#: Hard cap on a ``--workflow-yaml`` override file (#1002). A workflow override is
#: a small config; bounding the bytes BEFORE reading is the pragmatic guard against
#: a YAML alias/anchor-expansion DoS ("billion laughs"). ``yaml.safe_load`` blocks
#: arbitrary object construction but NOT alias expansion, and PyYAML has no built-in
#: alias cap — but anchors must be defined inline, so a byte cap bounds the blast
#: radius. 256 KiB is generous for any legitimate override.
_MAX_WORKFLOW_YAML_BYTES = 256 * 1024


def _parse_workflow_yaml(path: str) -> bool:
    """Parse a ``--workflow-yaml`` override file; return ``True`` when usable (#989).

    The file must exist, be within :data:`_MAX_WORKFLOW_YAML_BYTES` (#1002), and be
    a YAML mapping. A missing/oversized file or a parse error returns ``False`` (the
    caller maps that to exit 3 per spec §5.3). The parsed config is currently
    advisory — accepted and validated, not yet consumed.

    The size cap is checked via ``stat().st_size`` **before** the read, so an
    anchor/alias bomb (which ``yaml.safe_load`` would otherwise expand into
    gigabytes of in-memory lists) cannot exhaust memory — anchors are defined
    inline, so bounding the source bytes bounds the expansion.
    """
    import yaml  # noqa: PLC0415 — heavy-ish import, deferred to this rare path

    p = Path(path)
    if not p.is_file():
        return False
    if p.stat().st_size > _MAX_WORKFLOW_YAML_BYTES:
        return False
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return False
    return isinstance(data, dict)


def _load_tree_or_exit(run_id: str, state_dir: Path) -> WorkTree:
    """Load a persisted run's tree, exiting 2 on inconsistent/partial state (#1194).

    ``state_store.run_exists`` is a weak check (``tree.json`` presence only), so a
    run whose envelope was written but whose node files were not — the crash-mid-
    ``save_tree`` window the #990 consistency check exists to catch — passes
    ``run_exists`` yet makes ``load_tree`` raise ``ValueError`` (not
    ``FileNotFoundError``). The bare-resume + ``--start-from`` branches route
    through here so that surfaces as a clear ``Error:`` + exit 2 ("no usable run
    to resume") rather than a message-less uncaught crash (exit 1).
    """
    from primitives.work_orchestrator.shared import state_store  # noqa: PLC0415

    try:
        return state_store.load_tree(run_id, state_dir)
    except ValueError as exc:
        click.echo(
            f"Error: run {run_id!r} under {state_dir!s} is in an inconsistent/partial state "
            f"and cannot be resumed ({exc}). Remove that run dir to start the issue fresh.",
            err=True,
        )
        sys.exit(2)


def _apply_start_from(tree: WorkTree, start_from: str) -> None:
    """Mark every **leaf** before *start_from* (build order) ``skipped`` (#1001).

    Implements the ``--start-from NODE`` resume cursor against an already-decomposed
    (loaded) tree: every leaf that precedes NODE in the deterministic build-order
    walk — and is **not** within NODE's own subtree — is set ``skipped`` so the
    frontier resumes at NODE. NODE itself may be a leaf (resume there) or a split
    parent (resume into its subtree); either way, leaves at/after NODE are left
    untouched.

    Only **leaves** are skipped — never an ancestor split parent of NODE. Marking a
    ``blocked`` split parent ``skipped`` would be undone by :func:`tree.aggregate_parent`
    (which re-derives a split parent's status from its children) and could flip a
    subtree clean via the ``{"clean", "skipped"}`` aggregation rule, so the cursor
    must confine itself to leaf nodes (#1001 W2).

    A *start_from* that names no node in the tree raises ``KeyError`` — the caller
    maps that to a hard exit 2. A silent no-op would let a misspelled or
    not-yet-existing path masquerade as a successful mid-tree resume.
    """
    walk_paths = [n.path for n in tree.walk()]
    if start_from not in walk_paths:
        msg = f"--start-from node {start_from!r} is not in the run's tree"
        raise KeyError(msg)
    cutoff = walk_paths.index(start_from)
    before = set(walk_paths[:cutoff])
    subtree_prefix = f"{start_from}."
    for node in tree.leaves():
        # Skip a leaf only if it build-order-precedes NODE and is not part of
        # NODE's own subtree (a deeper leaf under a split NODE must still run).
        is_descendant = node.path == start_from or node.path.startswith(subtree_prefix)
        if node.path in before and not is_descendant and not node.is_terminal():
            node.status = "skipped"


def _finish(result: LegResult, *, output: str | None) -> None:
    """Map a LegResult to the documented exit code (spec §5.3) and emit output."""
    if result.branch_failed:
        # The branch Mode-1 stage failed and the run was halted before any prompt
        # was written (#1006) — surface the reason and exit 2 (no prompts against
        # the wrong branch).
        click.echo(f"Error: branch stage failed — {result.branch_reason}", err=True)
        sys.exit(2)
    if result.awaiting:
        click.echo(
            f"Awaiting judgments: wrote {result.prompts_path}. "
            "Judge it and re-invoke with --continue --judgments <round-N-judgments.json>.",
        )
        sys.exit(7)

    report_md = result.report_md or ""
    if output:
        Path(output).write_text(report_md, encoding="utf-8")
    else:
        click.echo(report_md)

    # Surface the Mode-1 branch / pr stage outcomes (#985).
    if result.branch_reason:
        click.echo(f"Branch stage: {result.branch_reason}", err=True)
    if result.pr_outcome is not None:
        if result.pr_outcome.opened:
            kind = "draft PR" if result.pr_outcome.draft else "PR"
            where = f": {result.pr_outcome.url}" if result.pr_outcome.url else ""
            click.echo(f"Opened {kind}{where}")
        else:
            click.echo(f"No PR opened — {result.pr_outcome.reason}", err=True)

    gate = result.gate
    recommendation = gate.recommendation if gate is not None else "incomplete"
    if recommendation == "pass":
        sys.exit(0)
    if gate is not None and _has_hard_failure(gate):
        # A hard stage failure halted a node (build / spec-review failure) → exit
        # 5, distinct from a clean review/fail gate (exit 4). Driven by the
        # tree-gate's has_hard_failure flag, which counts a failed node anywhere
        # in the tree — not just atomic leaves — so a node halted at spec-review
        # (before it is ever a leaf) still reaches exit 5 (#986).
        sys.exit(5)
    sys.exit(4)


def _has_hard_failure(gate: object) -> bool:
    """Return ``True`` when any node in the tree halted with a hard ``failed`` status.

    Reads the tree-gate's ``has_hard_failure`` flag (#986). A failed node need not
    be an atomic leaf — a spec-review failure halts a node before decomposition,
    so it never appears in ``leaf_verdicts``; the flag captures it regardless.
    """
    return bool(gate.has_hard_failure)  # type: ignore[attr-defined]

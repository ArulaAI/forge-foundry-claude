"""Forge Foundry — `forge-validate test-maintainer` subcommand.

Was inlined in `forge_validate/cli.py` before #101 / TOOL-2.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

import click

from forge_validate._cli_common import (
    continue_option,
    extract_only_option,
    judgments_option,
    state_dir_option,
    validate_git_ref,
    validate_skill_first_flags,
)
from primitives._sdet.config import SdetConfig
from primitives.test_maintainer import (
    MaintainerBudgetExceededError,
    propose_test_edits,
)
from primitives.test_maintainer.shared.apply import apply_proposals
from primitives.test_maintainer.shared.edit_proposal import EditProposal, ImpactSummary, Verdict
from primitives.test_maintainer.shared.maintainer import (
    _ZERO_DIFF_WARNING,
    write_dry_run_report,
)


@click.command("test-maintainer")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--diff-against",
    required=True,
    callback=validate_git_ref,
    help="Git ref to diff against (e.g., HEAD~1)",
)
@click.option(
    "--apply", "apply_flag", is_flag=True, default=False, help="Apply edits (default: dry-run)"
)
@click.option(
    "--open-pr",
    "open_pr_flag",
    is_flag=True,
    default=False,
    help=(
        "Branch + raise-PR mode (TM-6): when ≥1 verified-green edit exists, "
        "create forge/test-maintainer-<sha>, commit the green edits, push, and "
        "gh pr create a PR for human approval (never auto-merges). Zero green "
        "edits → report only, no branch/PR. Exits 8 (PR opened) or 9 (report only)."
    ),
)
@click.option(
    "--with-e2e",
    "with_e2e",
    is_flag=True,
    default=False,
    help=(
        "Verify impacted e2e specs by running them (opt-in; Playwright). Off by "
        "default — e2e is slow/flaky. When off, an accepted e2e edit is recorded "
        "'unverified — needs human e2e run' rather than applied as green (#1029)."
    ),
)
@click.option("--max-files", type=int, default=20, help="Cap on changed source files")
@click.option(
    "--concurrency",
    type=int,
    default=5,
    help="Max in-flight propose_edit LLM calls (default 5)",
)
@click.option("--format", "format_", default="markdown", type=click.Choice(["markdown", "json"]))
@click.option("--output", type=click.Path(), default=None, help="Report output path (dry-run mode)")
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0912, PLR0913, PLR0915
    path: str,
    diff_against: str,
    apply_flag: bool,
    open_pr_flag: bool,
    with_e2e: bool,
    max_files: int,
    concurrency: int,
    format_: str,
    output: str | None,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """Auto-propose test edits for the diff against REF.

    Exit codes (extends the conventional 0/1/2/7 vocabulary):

    \b
    0  Success (dry-run / apply with no PR requested).
    1  MaintainerBudgetExceededError or unexpected error.
    2  Input error (e.g. bad git ref — rejected by --diff-against).
    7  Awaiting judgments (--extract-only wrote a prompts file).
    8  PR opened — --open-pr created forge/test-maintainer-<sha> and ran
       gh pr create over ≥1 verified-green edit (never auto-merged).
    9  Report only — --open-pr was given but zero verified-green edits
       existed, so NO branch/PR was created; the report was emitted instead.
    """
    # The --continue requires --judgments enforcement runs via the shared guard.
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )

    project_root = Path(path).resolve()
    config = SdetConfig.from_project(project_root)
    # Override max-files from CLI
    config = config.model_copy(
        update={"s2": config.s2.model_copy(update={"max_files_per_run": max_files})}
    )

    # Resolve state directory (default: project_root/.state/)
    resolved_state_dir = Path(state_dir) if state_dir is not None else project_root / ".state"

    # =========================================================================
    # Branch: --extract-only (emit prompts, exit 7)
    # =========================================================================
    if extract_only:
        # Lazy imports to defer expensive module load until --extract-only path
        from primitives._sdet.depgraph import find_affected_tests  # noqa: PLC0415
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            PipelineState,
            save_intermediate,
            save_state,
        )
        from primitives._shared.protocol import (  # noqa: PLC0415
            PromptEnvelope,
            PromptItem,
            ResponseFormat,
            write_prompts,
        )
        from primitives.test_maintainer.shared.impact import classify_impact  # noqa: PLC0415
        from primitives.test_maintainer.shared.maintainer import (  # noqa: PLC0415
            _TEST_EXTENSIONS,
            _git_changed_files,
            _git_show_before,
            _is_test_path,
            _WorkUnit,
        )
        from primitives.test_maintainer.shared.proposer import (  # noqa: PLC0415
            _PROPOSAL_RESPONSE_SCHEMA,
            build_prompt_item_for_test,
        )

        # If mode is not "llm", deterministic mode: no prompts needed.
        # propose_test_edits() already emits the NOTICE + zero-diff WARNING.
        if config.s2.mode != "llm":
            from primitives.test_maintainer.shared.maintainer import (  # noqa: PLC0415
                diff_impact_counts,
            )

            accepted, dropped = propose_test_edits(
                project_root,
                diff_against=diff_against,
                config=config,
            )
            changed_files, source_changed_n = diff_impact_counts(project_root, diff_against)
            _emit_summary_and_report(
                accepted,
                dropped,
                project_root,
                apply_flag,
                output,
                format_,
                open_pr=open_pr_flag,
                diff_against=diff_against,
                impact_summary=ImpactSummary.empty(
                    mode=config.s2.mode,
                    diff_against=diff_against,
                    changed_files=changed_files,
                    source_changed=source_changed_n,
                ),
            )
            return

        # LLM mode: run impact analysis deterministically, build prompt items
        all_changed = _git_changed_files(project_root, diff_against)

        if not all_changed:
            click.echo(_ZERO_DIFF_WARNING.format(ref=diff_against), err=True)

        source_changed = [
            f for f in all_changed if f.suffix in _TEST_EXTENSIONS and not _is_test_path(f)
        ]

        if len(source_changed) > config.s2.max_files_per_run:
            raise MaintainerBudgetExceededError(
                f"Changed file count {len(source_changed)} exceeds "
                f"max_files_per_run={config.s2.max_files_per_run}. "
                "Narrow your diff or raise the cap in .foundry/sdet.yaml."
            )

        test_map = find_affected_tests(source_changed, project_root)

        work_units: list[_WorkUnit] = []
        for source_file, test_files in test_map.items():
            source_before = _git_show_before(project_root, diff_against, source_file)
            try:
                source_after = source_file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError, FileNotFoundError):
                continue

            for test_file in test_files:
                try:
                    test_content = test_file.read_text(encoding="utf-8")
                except (UnicodeDecodeError, PermissionError, FileNotFoundError):
                    continue

                impact = classify_impact(
                    source_before,
                    source_after,
                    test_content,
                    test_file,
                    source_extension=source_file.suffix,
                )
                if not impact.impacts:
                    continue

                work_units.append(_WorkUnit(impact, source_before, source_after, test_content))

        # Budget check
        if len(work_units) > config.llm.max_calls_per_run:
            from primitives.test_maintainer.shared.proposer import (  # noqa: PLC0415
                ProposerBudgetExceededError,
            )

            raise ProposerBudgetExceededError(
                f"Proposer LLM budget exceeded: {len(work_units)} impacted (source, test) "
                f"pairs, max={config.llm.max_calls_per_run}"
            )

        # No impacted test files: nothing to propose, exit 0. Explain WHY the closed
        # loop did not engage so an empty run is not mistaken for a bug (#1108).
        if not work_units:
            click.echo(
                f"S2 NOTICE: closed loop did not engage — {len(source_changed)} source "
                f"file(s) changed but no impacted tests were found to maintain "
                f"(changed_files={len(all_changed)}). No prompts written; exiting 0.",
                err=True,
            )
            accepted, dropped = [], []
            _emit_summary_and_report(
                accepted,
                dropped,
                project_root,
                apply_flag,
                output,
                format_,
                open_pr=open_pr_flag,
                diff_against=diff_against,
                impact_summary=ImpactSummary.empty(
                    mode=config.s2.mode,
                    diff_against=diff_against,
                    changed_files=len(all_changed),
                    source_changed=len(source_changed),
                ),
            )
            return

        # Build prompt items
        prompt_items: list[PromptItem] = []
        for wu in work_units:
            item_id, system, user = build_prompt_item_for_test(
                wu.impact, wu.source_before, wu.source_after, wu.test_content
            )
            prompt_items.append(
                PromptItem(
                    id=item_id,
                    system=system,
                    user=user,
                    response_format=ResponseFormat(
                        type="json",
                        schema=_PROPOSAL_RESPONSE_SCHEMA,
                    ),
                    model_tier="deep",
                )
            )

        envelope = PromptEnvelope(
            primitive="test-maintainer",
            round=1,
            items=prompt_items,
        )
        prompts_path = resolved_state_dir / "round-1-prompts.json"
        write_prompts(envelope, prompts_path)
        click.echo(f"WROTE {prompts_path}")

        # Save intermediates: impact reports + content pairs
        intermediates: list[dict[str, Any]] = []
        for wu in work_units:
            intermediates.append(
                {
                    "test_file": str(wu.impact.test_file),
                    "impacts": sorted(c.value for c in wu.impact.impacts),
                    "affected_functions": list(wu.impact.affected_functions),
                    "source_before": wu.source_before,
                    "source_after": wu.source_after,
                    "test_content": wu.test_content,
                }
            )
        save_intermediate("work_units", intermediates, resolved_state_dir)

        # Persist diff counts so --continue can report accurate ImpactSummary counts.
        save_intermediate(
            "diff_counts",
            {"changed_files": len(all_changed), "source_changed": len(source_changed)},
            resolved_state_dir,
        )

        save_state(
            PipelineState(
                primitive="test-maintainer",
                round=1,
                completed_stages=["impact_analysis"],
                next_stage="propose_continue",
                status="awaiting_judgments",
            ),
            resolved_state_dir,
        )

        sys.exit(7)

    # =========================================================================
    # Branch: --continue --judgments (resume after in-session judgment)
    # =========================================================================
    if continue_:
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            cleanup_state,
            load_intermediate,
        )
        from primitives._shared.protocol import read_judgments  # noqa: PLC0415
        from primitives.test_maintainer.shared.audit_gate import (  # noqa: PLC0415
            audit_proposals,
        )
        from primitives.test_maintainer.shared.impact import (  # noqa: PLC0415
            ImpactClass,
            ImpactReport,
        )
        from primitives.test_maintainer.shared.proposer import (  # noqa: PLC0415
            apply_proposal_judgment,
        )

        # Load saved work units
        intermediates = load_intermediate("work_units", resolved_state_dir)

        # Read judgments
        assert judgments is not None  # guaranteed by validate check at top
        judgment_envelope = read_judgments(Path(judgments))
        judgment_map = {item.id: item.response for item in judgment_envelope.items}

        # Apply judgments
        proposals = []
        for entry in intermediates:
            test_file = Path(entry["test_file"])
            impact = ImpactReport(
                test_file=test_file,
                impacts=frozenset(ImpactClass(v) for v in entry["impacts"]),
                affected_functions=tuple(entry["affected_functions"]),
            )
            item_id = f"narrative:{test_file}"
            response = judgment_map.get(item_id)
            if response is None:
                continue
            proposal = apply_proposal_judgment(impact, response, entry["test_content"])
            if proposal is not None:
                proposals.append(proposal)

        accepted, dropped = audit_proposals(proposals)

        # Reload diff counts saved by --extract-only so the summary is accurate.
        # load_intermediate raises PipelineStateNotFoundError (a ClickException)
        # if the intermediate is absent; suppress it for graceful degradation.
        from primitives._shared.pipeline_state import (  # noqa: PLC0415
            PipelineStateNotFoundError,
        )

        _diff_counts: dict[str, int] = {}
        with contextlib.suppress(PipelineStateNotFoundError):
            raw_counts = load_intermediate("diff_counts", resolved_state_dir)
            if isinstance(raw_counts, dict):
                _diff_counts = {
                    "changed_files": int(raw_counts.get("changed_files", 0)),
                    "source_changed": int(raw_counts.get("source_changed", 0)),
                }

        # Build an impact summary from the saved intermediates (llm mode)
        continue_summary = ImpactSummary(
            mode=config.s2.mode,
            diff_against=diff_against,
            changed_files=_diff_counts.get("changed_files", 0),
            source_changed=_diff_counts.get("source_changed", 0),
            impacted_tests=len(intermediates),
            coverage_measured=False,
            coverage_source="static",
        )

        # ------------------------------------------------------------------
        # Verify-loop → PR wiring (TM-10, #902).
        #
        # On --open-pr the audit-surviving ``accepted`` proposals are NOT yet
        # the verified-green set: they must pass the TM-5 strict green-gate
        # first.  Resolve the stack adapter, select the impacted subset from
        # ``impacted.json`` (falling back to the kept proposals' test files),
        # run ``verify_phase`` (write → run impacted subset → keep-green or
        # revert), and feed the resulting **verified-green ``kept`` set** to
        # the TM-6 ``--open-pr`` path (exit 8/9).
        #
        # Refine round structure: the in-session ``repropose`` (re-judge with
        # the failure text as context) is the SKILL.md body's job — it drives a
        # fresh ``--extract-only`` / ``--continue`` round.  At the CLI layer we
        # pass ``repropose=None`` (first-red reverts to needs-human), which is
        # consistent with the existing single-round extract→judge→continue
        # plumbing.  A multi-round re-judge is documented in the SKILL body.
        unverified_e2e: list[str] = []
        if _should_verify(apply_flag=apply_flag, open_pr_flag=open_pr_flag):
            accepted, unverified_e2e = _verify_accepted(
                accepted,
                project_root,
                resolved_state_dir,
                config=config,
                with_e2e=with_e2e,
            )

        # Clean up state
        cleanup_state(resolved_state_dir)

        _emit_summary_and_report(
            accepted,
            dropped,
            project_root,
            apply_flag,
            output,
            format_,
            open_pr=open_pr_flag,
            diff_against=diff_against,
            impact_summary=continue_summary,
            unverified_e2e=unverified_e2e,
        )
        return

    # =========================================================================
    # Branch: no skill-first flags — deterministic pre-flight only.
    # =========================================================================
    # In ``llm`` mode the model proposals come from the skill-first
    # ``--extract-only`` / ``--continue`` path; here we run the deterministic
    # diff + budget pre-flight, which yields no proposals.
    # propose_test_edits() emits the NOTICE (regex mode) + zero-diff WARNING.
    from primitives.test_maintainer.shared.maintainer import (  # noqa: PLC0415
        diff_impact_counts,
    )

    try:
        accepted, dropped = propose_test_edits(
            project_root,
            diff_against=diff_against,
            config=config,
        )
    except MaintainerBudgetExceededError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    changed_files, source_changed_n = diff_impact_counts(project_root, diff_against)
    _emit_summary_and_report(
        accepted,
        dropped,
        project_root,
        apply_flag,
        output,
        format_,
        open_pr=open_pr_flag,
        diff_against=diff_against,
        impact_summary=ImpactSummary.empty(
            mode=config.s2.mode,
            diff_against=diff_against,
            changed_files=changed_files,
            source_changed=source_changed_n,
        ),
    )


def _should_verify(*, apply_flag: bool, open_pr_flag: bool) -> bool:
    """Run the verify-by-running green-gate on ``--apply`` OR ``--open-pr`` (#1029).

    Previously gated on ``--open-pr`` only, so ``--apply`` applied
    C10/C11-audited-but-unverified edits. Now both apply the green-gate.
    """
    return apply_flag or open_pr_flag


def _e2e_spec_ids(state_dir: Path) -> frozenset[str]:
    """The test_ids the impact engine recorded with ``source="declared-e2e"``.

    These are e2e spec paths; an accepted edit whose ``test_file`` is one of them is
    an e2e edit (verified via Playwright under ``--with-e2e``, else ``unverified``).
    """
    import json as _json  # noqa: PLC0415

    impacted_path = state_dir / "impacted.json"
    if not impacted_path.is_file():
        return frozenset()
    try:
        entries = _json.loads(impacted_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    out: set[str] = set()
    for entry in entries if isinstance(entries, list) else []:
        if (
            isinstance(entry, dict)
            and entry.get("source") == "declared-e2e"
            and isinstance(entry.get("test_id"), str)
        ):
            out.add(entry["test_id"])
    return frozenset(out)


def _verify_accepted(
    accepted: list[EditProposal],
    project_root: Path,
    state_dir: Path,
    *,
    config: SdetConfig,
    with_e2e: bool = False,
) -> tuple[list[EditProposal], list[str]]:
    """Run the TM-5 green-gate over *accepted* (#1029); return (verified, unverified).

    Split the audit-surviving edits into **e2e** (``test_file`` is a ``declared-e2e``
    spec) and **unit/integration** (everything else):

    - The unit/integration subset is green-gated via the resolved stack adapter
      (``resolve_adapter``). Runs on ``--apply`` AND ``--open-pr`` now — previously
      ``--open-pr`` only (the adjacent fix).
    - The e2e subset is green-gated via the opt-in ``PlaywrightAdapter`` **only when**
      *with_e2e*; otherwise each e2e edit is recorded ``unverified`` (its test_id is
      returned, and it is NOT in the verified set — so it is neither applied/committed
      nor part of the PR's verified-green edits; exit 9 still fires when only
      unverified e2e edits exist).

    When no unit stack adapter recognises *project_root* (the universality fallback),
    the unit edits are returned unchanged (the in-session body owns verify). e2e is
    additive — it never blocks the unit path.
    """
    if not accepted:
        return accepted, []

    from primitives._sdet.stacks import resolve_adapter  # noqa: PLC0415
    from primitives._sdet.stacks.playwright import PlaywrightAdapter  # noqa: PLC0415
    from primitives.test_maintainer.shared.maintainer import verify_phase  # noqa: PLC0415

    e2e_ids = _e2e_spec_ids(state_dir)
    e2e_edits = [p for p in accepted if str(p.test_file) in e2e_ids]
    unit_edits = [p for p in accepted if str(p.test_file) not in e2e_ids]

    verified: list[EditProposal] = []
    unverified: list[str] = []

    # --- unit / integration subset (resolved stack adapter) ---
    if unit_edits:
        adapter = resolve_adapter(project_root)
        if adapter is None:
            # No runnable stack adapter — the in-session body owns verify.
            verified.extend(unit_edits)
        else:
            subset, subset_for = _build_verify_subset(unit_edits, state_dir)
            outcome = verify_phase(
                adapter,
                project_root,
                unit_edits,
                subset=subset,
                subset_for=subset_for,
                run_dir=state_dir,
                config=config,
                repropose=None,
            )
            verified.extend(outcome.kept)

    # --- e2e subset (opt-in Playwright) ---
    if e2e_edits:
        if with_e2e:
            subset, subset_for = _build_verify_subset(e2e_edits, state_dir)
            outcome = verify_phase(
                PlaywrightAdapter(),
                project_root,
                e2e_edits,
                subset=subset,
                subset_for=subset_for,
                run_dir=state_dir,
                config=config,
                repropose=None,
            )
            verified.extend(outcome.kept)
        else:
            # e2e verify off: record as unverified (not applied / not in PR set).
            unverified.extend(str(p.test_file) for p in e2e_edits)

    return verified, unverified


def _build_verify_subset(
    accepted: list[EditProposal],
    state_dir: Path,
) -> tuple[Any, Any]:
    """Select the impacted test subset to run during verify (TM-10).

    Returns ``(default_subset, subset_for)``: a global *default_subset* (all
    impacted ids) plus a **per-proposal** override keyed by the proposal's
    ``test_file`` string.  Each edit is verified against the impacted tests of
    *its own* test file so a not-yet-applied sibling edit cannot poison another
    edit's green-gate (the edits are applied one at a time).  Running the whole
    file's tests — not just the single edited node — is still the regression
    guard (design spec §6).

    The stack-native test ids come from ``impacted.json`` under *state_dir* (the
    impact engine's output); a proposal whose test file has no recorded ids
    falls back to the test-file path itself (pytest collects a file path as a
    node-id container).
    """
    import json as _json  # noqa: PLC0415

    from primitives._sdet.stacks.contract import TestSubset  # noqa: PLC0415

    # Map repo-relative test-file → its impacted stack-native test ids.
    by_file: dict[str, set[str]] = {}
    impacted_path = state_dir / "impacted.json"
    if impacted_path.is_file():
        try:
            entries = _json.loads(impacted_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = []
        for entry in entries if isinstance(entries, list) else []:
            tid = entry.get("test_id") if isinstance(entry, dict) else None
            if isinstance(tid, str) and tid:
                # A pytest node-id is ``<file>::<test>``; the file portion keys it.
                test_file = tid.split("::", 1)[0]
                by_file.setdefault(test_file, set()).add(tid)

    subset_for: dict[str, TestSubset] = {}
    all_ids: set[str] = set()
    for p in accepted:
        tf = str(p.test_file)
        ids = by_file.get(tf)
        if not ids:
            # Fall back to the proposal's test-file path (pytest accepts a path).
            ids = {tf}
        subset_for[tf] = TestSubset(test_ids=frozenset(ids))
        all_ids |= ids

    return TestSubset(test_ids=frozenset(all_ids)), subset_for


def _emit_summary_and_report(
    accepted: list[EditProposal],
    dropped: list[tuple[EditProposal, list[str]]],
    project_root: Path,
    apply_flag: bool,
    output: str | None,
    format_: str,
    *,
    open_pr: bool = False,
    diff_against: str = "HEAD",
    impact_summary: ImpactSummary | None = None,
    unverified_e2e: list[str] | None = None,
) -> None:
    """Print summary and either apply edits, open a PR, or write a dry-run report.

    When *open_pr* is set, the *accepted* proposals are the verified-green set
    (in skill-first mode the in-session verify loop ran before ``--continue``):
    ≥1 → branch + commit + push + ``gh pr create`` (exit 8); zero → report
    only, no branch/PR (exit 9).

    *impact_summary* is threaded to ``write_dry_run_report`` so every report
    (including empty ones) carries the mode / diff / coverage-source context.
    *unverified_e2e* lists e2e spec edits proposed but not green-verified (e2e
    verify off, #1029) — surfaced so the engineer runs them by hand.
    """
    n = len(accepted)
    updates = sum(1 for p in accepted if p.verdict == Verdict.UPDATE)
    adds = sum(1 for p in accepted if p.verdict == Verdict.ADD)
    deletes = sum(1 for p in accepted if p.verdict == Verdict.DELETE)

    # --format json (#1109): stdout must be a single parseable JSON document, not the
    # human summary text. In markdown mode (default) the human lines are unchanged.
    as_json = format_ == "json"
    summary: dict[str, object] = {
        "accepted": n,
        "updates": updates,
        "adds": adds,
        "deletes": deletes,
        "dropped": len(dropped),
    }
    if unverified_e2e:
        summary["unverified_e2e"] = sorted(unverified_e2e)

    if not as_json:
        click.echo(
            f"{n} accepted; {updates} updates, {adds} adds, {deletes} deletes. "
            f"{len(dropped)} dropped."
        )
        if unverified_e2e:
            click.echo(
                f"{len(unverified_e2e)} unverified e2e edit(s) — needs human e2e run "
                f"(pass --with-e2e to green-gate them): {', '.join(sorted(unverified_e2e))}"
            )

    if open_pr:
        # The --open-pr path has its own exit-8/9 status output; JSON mode is not
        # supported there (it is a branch/PR side-effecting flow, not a report).
        _open_pr_or_report(accepted, project_root, diff_against=diff_against)
        return

    if apply_flag:
        if accepted:
            touched = apply_proposals(project_root, accepted)
            summary["applied"] = len(touched)
            if not as_json:
                click.echo(f"Applied {len(touched)} test edits.")
        else:
            summary["applied"] = 0
            if not as_json:
                click.echo("No proposals to apply.")
        if as_json:
            import json as _json  # noqa: PLC0415

            click.echo(_json.dumps(summary))
        return

    out_path = Path(output) if output else None
    target = write_dry_run_report(
        project_root,
        accepted,
        dropped,
        output_path=out_path,
        format_=format_,
        impact_summary=impact_summary,
    )
    if as_json:
        import json as _json  # noqa: PLC0415

        summary["report"] = str(target)
        if impact_summary is not None:
            summary["impact_summary"] = impact_summary.to_dict()
        click.echo(_json.dumps(summary))
    else:
        click.echo(f"Wrote report to {target}")


def _open_pr_or_report(
    accepted: list[EditProposal],
    project_root: Path,
    *,
    diff_against: str,
) -> None:
    """Branch + raise-PR mode (TM-6) — exits 8 (PR opened) or 9 (report only).

    The *accepted* proposals are the verified-green edits.  ≥1 → branch +
    commit + push + ``gh pr create`` (exit 8).  Zero → no branch/PR; the
    report-only path (exit 9).  Never auto-merges.
    """
    from primitives.test_maintainer.shared.pr import (  # noqa: PLC0415
        PrInputError,
        open_pr_for_green_edits,
    )
    from primitives.test_maintainer.shared.verify import VerifyOutcome  # noqa: PLC0415

    if not accepted:
        click.echo("No verified-green edits — no branch/PR opened (report only).")
        sys.exit(9)

    outcome = VerifyOutcome(kept=list(accepted))
    impacted = sorted({str(p.test_file) for p in accepted})
    try:
        result = open_pr_for_green_edits(
            project_root,
            outcome,
            diff_against=diff_against,
            impacted=impacted,
        )
    except PrInputError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    if result.opened_pr:
        url = result.pr_url or "(URL not reported)"
        click.echo(f"Opened PR on branch {result.branch}: {url}")
        sys.exit(8)

    # Green edits existed but a git/gh step failed — surface and exit 1.
    click.echo(f"Error: could not open PR: {result.error}", err=True)
    sys.exit(1)

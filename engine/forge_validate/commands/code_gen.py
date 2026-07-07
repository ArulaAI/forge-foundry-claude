"""Forge Foundry — `forge-validate code-gen` subcommand (C3 PLB).

Extract leg: parse the C2 plan, isolate a branch, gather per-task context,
write authoring prompts, exit 7. Continue leg: apply the model's change-set,
format, run the test, retry on failure within the bound, render the report.
The CLI never calls an LLM.
"""

from __future__ import annotations

import datetime
import json
import subprocess
import sys
from pathlib import Path

import click

from forge_validate._cli_common import (
    continue_option,
    emit_report,
    extract_only_option,
    judgments_option,
    state_dir_option,
    validate_skill_first_flags,
)
from primitives.code_gen.shared.applier import AnchorError
from primitives.code_gen.shared.changeset import ChangeSet, parse_changeset
from primitives.code_gen.shared.engine import (
    DirtyTreeError,
    PlanError,
    PlanTask,
    TaskResult,
    apply_and_test,
    build_authoring_prompts,
    ensure_clean_repo,
    isolate_branch,
    make_failure_delta,
    no_op_reason,
    parse_plan,
    plan_slug,
    process_exit_code,
    render_report,
    task_has_uncommitted,
)


@click.command(name="code-gen")
@click.option("--plan", "plan_path", required=True, type=click.Path())
@click.option("--codebase-root", "root", default=".", type=click.Path())
@click.option("--task-id", default=None)
@click.option("--no-branch", is_flag=True, default=False)
@click.option("--auto-commit", is_flag=True, default=False)
@click.option("--max-retries", default=3, type=int)
@click.option("--stack", default="python")
@click.option("--format", "fmt", default="markdown")
@extract_only_option()
@continue_option()
@judgments_option()
@state_dir_option()
def cli(  # noqa: PLR0913 — the documented CLI surface (spec §5)
    plan_path: str,
    root: str,
    task_id: str | None,
    no_branch: bool,
    auto_commit: bool,
    max_retries: int,
    stack: str,
    fmt: str,
    extract_only: bool,
    continue_: bool,
    judgments: str | None,
    state_dir: str | None,
) -> None:
    """C3 code-gen: extract authoring prompts or apply a change-set."""
    validate_skill_first_flags(
        extract_only=extract_only,
        continue_=continue_,
        judgments=judgments,
    )
    plan_file = Path(plan_path)
    if not plan_file.is_file():
        click.echo(f"plan not found: {plan_path}", err=True)
        sys.exit(2)
    root_path = Path(root)
    try:
        tasks = parse_plan(plan_file.read_text())
    except PlanError as exc:
        click.echo(f"unparseable plan: {exc}", err=True)
        sys.exit(3)
    if task_id:
        tasks = [t for t in tasks if t.id == task_id]
        if not tasks:
            click.echo(f"task not found: {task_id}", err=True)
            sys.exit(2)

    state = Path(state_dir) if state_dir else root_path / ".state"
    state.mkdir(parents=True, exist_ok=True)

    if extract_only:
        if not no_branch:
            try:
                ensure_clean_repo(root_path)
            except DirtyTreeError as exc:
                click.echo(str(exc), err=True)
                sys.exit(1)
            isolate_branch(root_path, plan_slug(plan_file.name), _timestamp())
        prompts = build_authoring_prompts(tasks, root_path)
        (state / "authoring_prompts.json").write_text(
            json.dumps(
                [
                    {
                        "task_id": p.task_id,
                        "files": p.files,
                        "current": p.current,
                        "intent": p.intent,
                    }
                    for p in prompts
                ],
                indent=2,
            )
        )
        click.echo(f"wrote {len(prompts)} authoring prompt(s); awaiting judgments", err=True)
        sys.exit(7)

    if continue_:
        assert judgments is not None  # guaranteed by validate_skill_first_flags
        cs = parse_changeset(Path(judgments).read_text())
        task = next(t for t in tasks if t.id == cs.task_id)
        if task_has_uncommitted(task, root_path):
            result: TaskResult = TaskResult(task.id, "skipped_uncommitted", 0, "")
        else:
            result = _run_task(cs, task, root_path, stack, max_retries, auto_commit, state)
        emit_report(render_report([result], fmt))
        sys.exit(process_exit_code([result]))


def _timestamp() -> str:
    """Return a UTC timestamp string for branch naming.

    Uses timezone-aware datetime to satisfy the DTZ003 lint rule.
    Tests pass --no-branch so this path is not exercised in the test suite,
    but it must lint clean.
    """
    return datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%S")


def _run_task(
    cs: ChangeSet,
    task: PlanTask,
    root: Path,
    stack: str,
    max_retries: int,
    auto_commit: bool,
    state: Path,
) -> TaskResult:
    """Apply the change-set, run tests, write a revise prompt on failure.

    The PLB protocol expects each revise round to be a *separate* --continue
    invocation: the harness reads revise_<id>.json, the model emits a patch
    change-set, and re-invokes --continue. This function implements the
    single-attempt-then-write-revise-prompt shape; the multi-invocation revise
    loop is driven by SKILL.md (Task 9). The CLI never calls an LLM.
    """
    attempts = 0
    test_output = ""
    current_cs = cs
    while attempts < max_retries:
        attempts += 1
        try:
            ok, test_output, rep = apply_and_test(current_cs, task, root, stack=stack, use_git=True)
        except AnchorError as exc:
            # A zero-match or non-unique edit anchor is a normal expected failure;
            # record it as a revise prompt so the harness can re-invoke --continue
            # with a corrected change-set rather than crashing the CLI.
            (state / f"revise_{task.id}.json").write_text(
                json.dumps(
                    {
                        "task_id": task.id,
                        "error": "anchor error",
                        "message": str(exc),
                    },
                    indent=2,
                )
            )
            return TaskResult(task.id, "aborted_max_retries", attempts, str(exc))
        dropped = [f"{d.path}: {d.reason}" for d in rep.dropped]
        if not rep.applied:
            # A code-gen task that writes nothing is never a success — most often
            # the plan task omitted its `- Create:/Modify:` file lines, so the
            # allowlist was empty and every op was dropped. Fail loudly (takes
            # precedence over the test result: with a test command an empty apply
            # would otherwise surface as an ambiguous aborted_max_retries) instead
            # of reporting a silent 0-op "pass" (#1193).
            return TaskResult(
                task.id,
                "no_ops_applied",
                attempts,
                no_op_reason(task, current_cs, rep),
                dropped=dropped,
            )
        if ok:
            sha = _maybe_commit(task, root, auto_commit)
            return TaskResult(
                task.id, "passed", attempts, test_output, commit_sha=sha, dropped=dropped
            )
        # Write a revise prompt for the next round; the harness re-invokes
        # --continue with the new change-set.  For the single-shot in-process
        # bound we break and report aborted once exhausted.
        delta = make_failure_delta(task, root, test_output)
        (state / f"revise_{task.id}.json").write_text(
            json.dumps(
                {
                    "task_id": delta.task_id,
                    "test_output": delta.test_output,
                    "current_regions": delta.current_regions,
                },
                indent=2,
            )
        )
        break
    return TaskResult(task.id, "aborted_max_retries", attempts, test_output)


def _maybe_commit(task: PlanTask, root: Path, auto_commit: bool) -> str | None:
    """Commit task files if --auto-commit is set and the task has a commit message."""
    if not auto_commit or not task.commit_msg:
        return None
    subprocess.run(  # noqa: S603
        ["git", "add", "--", *task.files],  # noqa: S607
        cwd=root,
        check=True,
        timeout=60,
    )
    subprocess.run(  # noqa: S603
        ["git", "commit", "-m", task.commit_msg],  # noqa: S607
        cwd=root,
        check=True,
        timeout=60,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.stdout.strip()

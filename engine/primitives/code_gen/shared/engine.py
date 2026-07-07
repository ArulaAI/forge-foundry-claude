"""Orchestration engine for the C3 code_gen PLB primitive.

Part 1: C2-plan parsing + extract-leg authoring-prompt construction.
Parts 2 & 3 (Tasks 5, 6) add the continue pipeline and report rendering.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from primitives.code_gen.shared.applier import (  # noqa: F401
    AnchorError,
    ApplyReport,
    apply_changeset,
)
from primitives.code_gen.shared.format import format_files

if TYPE_CHECKING:
    from primitives.code_gen.shared.changeset import ChangeSet


class PlanError(ValueError):
    """The plan could not be parsed into any task (maps to exit 3)."""


@dataclass(frozen=True)
class PlanTask:
    id: str
    title: str
    files: list[str]
    test_cmd: str | None = None
    commit_msg: str | None = None


_TASK_HEADER = re.compile(r"^###\s+([A-Za-z0-9.\-]+):\s*(.+)$", re.MULTILINE)
_FILE_LINE = re.compile(r"^- (?:Create|Modify|Test|Delete):\s*`([^`]+)`", re.MULTILINE)
_TEST_CMD = re.compile(r"^Test command:\s*`([^`]+)`", re.MULTILINE)
_COMMIT = re.compile(r"^Commit:\s*`([^`]+)`", re.MULTILINE)


def parse_plan(text: str) -> list[PlanTask]:
    headers = list(_TASK_HEADER.finditer(text))
    if not headers:
        raise PlanError("no '### <id>: <title>' task headers found in plan")
    tasks: list[PlanTask] = []
    for i, m in enumerate(headers):
        start = m.end()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        body = text[start:end]
        files = [re.sub(r":\d+(-\d+)?$", "", f) for f in _FILE_LINE.findall(body)]
        test_cmd = tc.group(1) if (tc := _TEST_CMD.search(body)) else None
        commit = c.group(1) if (c := _COMMIT.search(body)) else None
        tasks.append(PlanTask(m.group(1), m.group(2).strip(), files, test_cmd, commit))
    return tasks


def plan_slug(filename: str) -> str:
    stem = Path(filename).stem
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40]


@dataclass(frozen=True)
class AuthoringPrompt:
    task_id: str
    files: list[str]
    current: dict[str, str]
    intent: str


def build_authoring_prompts(tasks: list[PlanTask], root: Path) -> list[AuthoringPrompt]:
    prompts: list[AuthoringPrompt] = []
    for t in tasks:
        current: dict[str, str] = {}
        for f in t.files:
            p = root / f
            if p.is_file():
                current[f] = p.read_text()
        prompts.append(AuthoringPrompt(t.id, t.files, current, t.title))
    return prompts


# ---------------------------------------------------------------------------
# Part 2: continue pipeline — apply / format / test / delta
# ---------------------------------------------------------------------------

_TEST_OUTPUT_CAP = 4000


@dataclass(frozen=True)
class TaskResult:
    task_id: str
    outcome: str
    attempts: int
    test_output: str
    commit_sha: str | None = None
    dropped: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FailureDelta:
    task_id: str
    test_output: str
    current_regions: dict[str, str]


def run_test(cmd: str, root: Path) -> tuple[bool, str]:
    proc = subprocess.run(  # noqa: S603
        shlex.split(cmd), cwd=root, capture_output=True, text=True, timeout=600, check=False
    )
    out = (proc.stdout + proc.stderr).strip()[-_TEST_OUTPUT_CAP:]
    return proc.returncode == 0, out


def apply_and_test(
    cs: ChangeSet,
    task: PlanTask,
    root: Path,
    stack: str,
    use_git: bool,  # noqa: FBT001
) -> tuple[bool, str, ApplyReport]:
    rep = apply_changeset(cs, root, set(task.files), use_git=use_git)
    fmt = format_files(stack, root, sorted(rep.touched))
    if not fmt.ok:
        return False, f"formatter parse error:\n{fmt.error}", rep
    if not task.test_cmd:
        return True, "", rep
    ok, out = run_test(task.test_cmd, root)
    return ok, out, rep


def make_failure_delta(task: PlanTask, root: Path, test_output: str) -> FailureDelta:
    regions: dict[str, str] = {}
    for f in task.files:
        p = root / f
        if p.is_file():
            regions[f] = p.read_text()
    return FailureDelta(task.id, test_output[-_TEST_OUTPUT_CAP:], regions)


# ---------------------------------------------------------------------------
# Part 3: git safety, exit-code precedence, report rendering
# ---------------------------------------------------------------------------


class DirtyTreeError(Exception):
    """The working tree has uncommitted changes; refuse to run."""


def ensure_clean_repo(root: Path) -> None:
    proc = subprocess.run(
        ["git", "status", "--porcelain"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise DirtyTreeError("not a git repository")
    if proc.stdout.strip():
        raise DirtyTreeError("working tree is not clean")


def isolate_branch(root: Path, slug: str, timestamp: str) -> str:
    branch = f"forge-code-gen-{slug}-{timestamp}"
    subprocess.run(["git", "checkout", "-b", branch], cwd=root, check=True, timeout=60)  # noqa: S603,S607
    return branch


def task_has_uncommitted(task: PlanTask, root: Path) -> bool:
    existing = [f for f in task.files if (root / f).is_file()]
    if not existing:
        return False
    proc = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain", "--", *existing],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return bool(proc.stdout.strip())


def no_op_reason(task: PlanTask, cs: ChangeSet, rep: ApplyReport) -> str:
    """Explain why a task applied zero ops (#1193).

    A code-gen task that produces no file changes is never a success — it is
    either a plan-grammar gap (no ``- Create:/Modify:`` file lines, so the
    allowlist is empty and every op is dropped), an empty change-set, or a
    change-set whose every op fell outside the allowlist / containment guard.
    """
    if not task.files:
        return (
            f"0 ops applied — task '{task.id}' declared no `- Create:`/`- Modify:` file lines, "
            "so its allowlist is empty and every change-set op was dropped. Add the file lines "
            "(e.g. `- Create: `path/to/file.py``) to the task body."
        )
    if not cs.ops:
        return f"0 ops applied — the change-set for task '{task.id}' contained no ops"
    dropped = "; ".join(f"{d.path} ({d.reason})" for d in rep.dropped) or "unknown"
    return f"0 ops applied — all {len(cs.ops)} change-set op(s) were dropped: {dropped}"


def process_exit_code(results: list[TaskResult]) -> int:
    if any(r.outcome in ("aborted_max_retries", "no_ops_applied") for r in results):
        return 4
    if any(r.outcome == "skipped_uncommitted" for r in results):
        return 5
    return 0


def render_report(results: list[TaskResult], fmt: str = "markdown") -> str:
    if fmt == "json":
        return json.dumps(
            [
                {
                    "task_id": r.task_id,
                    "outcome": r.outcome,
                    "attempts": r.attempts,
                    "test_output": r.test_output,
                    "commit_sha": r.commit_sha,
                    "dropped": r.dropped,
                }
                for r in results
            ],
            indent=2,
        )
    lines = ["# code-gen report", "", "| Task | Outcome | Attempts | Commit |", "|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.task_id} | {r.outcome} | {r.attempts} | {r.commit_sha or '—'} |")
    # Surface no-op failures and dropped ops loudly (#1193) — a bare outcome
    # column is easy to miss; the reason/dropped detail must be visible.
    notes = [r for r in results if r.outcome == "no_ops_applied" or r.dropped]
    if notes:
        lines += ["", "## Notes"]
        for r in notes:
            if r.outcome == "no_ops_applied":
                lines.append(f"- ⚠️ **{r.task_id}**: {r.test_output}")
            for d in r.dropped:
                lines.append(f"- {r.task_id} dropped op: {d}")
    return "\n".join(lines) + "\n"

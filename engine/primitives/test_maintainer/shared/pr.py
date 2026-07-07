"""pr.py — the branch + raise-PR mode (S2 TM-6, #898).

The final stage of the S2 closed loop (design spec
``docs/superpowers/specs/2026-06-19-test-maintainer-closed-loop-design.md``
§7).  After the TM-5 verify loop (``verify.py``) produces a
:class:`~primitives.test_maintainer.shared.verify.VerifyOutcome`, this module
turns the **verified-green** edits into a pull request for human approval:

1. ``git checkout -b forge/test-maintainer-<short-sha>``
2. write + ``git add`` ONLY the verified-green edits (the verify loop's
   ``kept`` set — already on disk from the loop, re-asserted here)
3. ``git commit`` the green edits
4. ``git push -u origin <branch>``
5. ``gh pr create`` with a body = summary + Needs-human + provenance

**Strict green-gate.**  Zero verified-green edits → **no branch, no PR**: the
report is emitted (and, in CI, optionally a ``gh pr comment``) and the function
returns ``opened_pr=False``.  An empty PR is never opened.

**Never auto-merges.**  This module only ever *opens* a PR; merge is a human
action.

**Hard guard preserved.**  Before any git write, every kept proposal is checked
against ``apply._is_test_path`` — a non-test path is a hard ``ValueError`` (the
same rule ``apply.py`` / ``verify.py`` enforce).  ``test_maintainer`` may never
commit a production-source path.

**SEC-8.**  Every value that reaches a ``git`` / ``gh`` argv is regex-validated
CLI-side with the first character constrained so a leading ``-`` cannot be
parsed as a flag, ``shell=True`` is never used, and argv is always a list.  The
branch name is built from a *sanitized short-sha* (``[0-9a-f]{7,40}``), not free
user input — so the branch name is structurally safe by construction.  The diff
ref + PR title interpolation are validated against the same discipline.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from primitives.test_maintainer.shared.apply import _is_test_path
from primitives.test_maintainer.shared.edit_proposal import Verdict

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.test_maintainer.shared.edit_proposal import EditProposal
    from primitives.test_maintainer.shared.verify import NeedsHuman, VerifyOutcome

# ---------------------------------------------------------------------------
# SEC-8 validators. Each pattern constrains the FIRST character to a safe set
# so a value can never be parsed as a flag by git/gh (a leading ``-``), and the
# body admits only the characters the field legitimately uses.
# ---------------------------------------------------------------------------

#: A git short-sha (or full sha): 7-40 lowercase hex chars, nothing else. The
#: branch name is built from this, so the branch name is safe by construction.
_VALID_SHORT_SHA = re.compile(r"^[0-9a-f]{7,40}$")

#: A git ref (matches the CLI ``--diff-against`` mirror): first char excludes
#: ``-``; body admits the ref alphabet.
_VALID_GIT_REF = re.compile(r"^[A-Za-z0-9._/~^@:][A-Za-z0-9._/~^@{}:-]*$")

#: A PR title line: first char is alphanumeric (no leading ``-``); the body
#: admits ordinary title text but NO control chars, ``$`` or backtick (so the
#: value can't carry a newline / shell-expansion vector into the argv).
_VALID_PR_TITLE = re.compile(r"^[A-Za-z0-9][^\x00-\x1f$`]*$")

#: The branch prefix. The full branch name is ``<prefix><short-sha>``.
_BRANCH_PREFIX = "forge/test-maintainer-"

_DEFAULT_REMOTE = "origin"
_DEFAULT_BASE = "main"


class PrInputError(ValueError):
    """A value bound for a git/gh argv failed SEC-8 validation."""


def _validate_short_sha(value: str) -> str:
    if not _VALID_SHORT_SHA.match(value):
        raise PrInputError(f"invalid short-sha for branch name: {value!r}")
    return value


def _validate_git_ref(value: str) -> str:
    if not _VALID_GIT_REF.match(value):
        raise PrInputError(f"invalid git ref: {value!r}")
    return value


def _validate_pr_title(value: str) -> str:
    if not _VALID_PR_TITLE.match(value):
        raise PrInputError(f"invalid PR title: {value!r}")
    return value


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` (list argv, no shell). Never raises on non-zero."""
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` (list argv, no shell). Never raises on non-zero."""
    return subprocess.run(  # noqa: S603
        ["gh", *args],  # noqa: S607
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _short_sha(project_root: Path) -> str:
    """Return the current HEAD's sanitized short-sha (or ``"0000000"``).

    The value is validated against :data:`_VALID_SHORT_SHA` before use, so the
    derived branch name is structurally safe regardless of repo state.
    """
    result = _git(["rev-parse", "--short=7", "HEAD"], project_root)
    sha = result.stdout.strip().lower()
    if result.returncode != 0 or not _VALID_SHORT_SHA.match(sha):
        return "0000000"
    return sha


def branch_name_for(short_sha: str) -> str:
    """Build the branch name ``forge/test-maintainer-<short-sha>``.

    *short_sha* is validated (SEC-8) so the returned name cannot embed a
    metacharacter or a leading ``-``.
    """
    return f"{_BRANCH_PREFIX}{_validate_short_sha(short_sha)}"


@dataclass(frozen=True)
class PrResult:
    """The outcome of :func:`open_pr_for_green_edits`.

    ``opened_pr`` is False in the zero-green case (report only) and True when a
    branch was created, committed, pushed, and ``gh pr create`` ran.  The other
    fields are populated only when ``opened_pr`` is True.
    """

    opened_pr: bool
    branch: str = ""
    pr_url: str = ""
    committed: tuple[str, ...] = ()
    pr_body: str = ""
    #: Distinct from ``opened_pr``: True when the green-gate produced ≥1 edit
    #: but a git/gh step failed (e.g. push rejected). The PR body is still
    #: returned for the report path.
    error: str = ""


def _summary_line(kept: list[EditProposal]) -> str:
    updates = sum(1 for p in kept if p.verdict == Verdict.UPDATE)
    adds = sum(1 for p in kept if p.verdict == Verdict.ADD)
    deletes = sum(1 for p in kept if p.verdict == Verdict.DELETE)
    return (
        f"{len(kept)} applied (verified green); {updates} updates, {adds} adds, {deletes} deletes"
    )


def render_pr_body(
    outcome: VerifyOutcome,
    *,
    diff_against: str,
    impacted: list[str] | None = None,
    coverage_source: dict[str, str] | None = None,
    unclosed_gaps: list[str] | None = None,
) -> str:
    """Render the PR body (design spec §7).

    Sections:

    - **Summary**: ``N applied (verified green); A updates, B adds, C deletes``.
    - **Needs human attention**: the verify loop's ``needs_human`` (un-fixable
      tests + last failure) plus any *unclosed_gaps* (un-closed S1 gaps).
    - **Provenance**: the diff ref, the impacted set, and the
      coverage|static source per test.

    *diff_against* is validated (SEC-8) before interpolation — it is the only
    externally-influenced value rendered into the body.
    """
    diff_against = _validate_git_ref(diff_against)
    lines: list[str] = [
        "## Summary",
        "",
        _summary_line(outcome.kept),
        "",
        "## Needs human attention",
        "",
    ]
    needs: list[NeedsHuman] = outcome.needs_human
    gaps = unclosed_gaps or []
    if not needs and not gaps:
        lines.append("_None — every impacted test was made green._")
    else:
        for nh in needs:
            last = nh.last_failure.strip() or "(no failure captured)"
            lines.append(f"- **{nh.test_id}** — {nh.reason}")
            lines.append(f"  - last failure: `{last.splitlines()[0]}`")
        for gap in gaps:
            lines.append(f"- **S1 gap (not auto-closed)** — {gap}")
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- Diff ref: `{diff_against}`")
    if impacted:
        lines.append(f"- Impacted set ({len(impacted)}): {', '.join(sorted(impacted))}")
    else:
        lines.append("- Impacted set: (none recorded)")
    if coverage_source:
        lines.append("- Per-test source:")
        for test_id in sorted(coverage_source):
            lines.append(f"  - `{test_id}`: {coverage_source[test_id]}")
    lines.append("")
    lines.append(
        "> This PR was opened by `test_maintainer` for **human approval** and is never auto-merged."
    )
    lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True)
class _Cmd:
    """One executed command, for the dry-run / test seam."""

    tool: str
    args: tuple[str, ...]


def open_pr_for_green_edits(  # noqa: PLR0913
    project_root: Path,
    outcome: VerifyOutcome,
    *,
    diff_against: str,
    pr_title: str | None = None,
    base: str = _DEFAULT_BASE,
    remote: str = _DEFAULT_REMOTE,
    commit_message: str | None = None,
    impacted: list[str] | None = None,
    coverage_source: dict[str, str] | None = None,
    unclosed_gaps: list[str] | None = None,
    dry_run: bool = False,
    recorder: list[_Cmd] | None = None,
) -> PrResult:
    """Branch + commit-green + push + ``gh pr create`` (strict green-gate).

    If ``outcome.kept`` is empty → **no branch, no PR**: returns
    ``PrResult(opened_pr=False)`` (report-only).  Otherwise:

    1. Validate every kept proposal is a test path (hard ``ValueError`` else).
    2. ``git checkout -b forge/test-maintainer-<short-sha>``.
    3. Materialise + ``git add`` each green edit; ``git commit``.
    4. ``git push -u <remote> <branch>``.
    5. ``gh pr create --base <base> --head <branch> --title … --body-file …``.

    Args:
        project_root: The consumer repo root (a git work tree).
        outcome: The TM-5 verify outcome; only ``kept`` edits are committed.
        diff_against: The diff ref (SEC-8 validated; rendered in the body).
        pr_title: PR title (SEC-8 validated). Default derives from the summary.
        base / remote: PR base branch / push remote. ``git``-side defaults.
        commit_message: Commit message. Default ``test(maintainer): …``.
        impacted / coverage_source / unclosed_gaps: provenance for the body.
        dry_run: When True, validate + render + record the argv WITHOUT running
            git/gh (used by ``--apply`` previews and by tests that don't want a
            real ``gh``).
        recorder: When given, every command's argv is appended (test seam).

    Returns:
        A :class:`PrResult`. ``opened_pr`` distinguishes "PR opened" from
        "report only / zero green".

    Raises:
        PrInputError: a value bound for git/gh argv failed SEC-8 validation.
        ValueError: a kept proposal targets a non-test path (hard guard).
    """
    kept = outcome.kept

    # --- Strict green-gate: zero green edits → no branch, no PR. ------------
    if not kept:
        return PrResult(opened_pr=False, pr_body="")

    # --- Hard guard: never commit a non-test path. -------------------------
    for p in kept:
        if not _is_test_path(p.test_file):
            raise ValueError(
                f"Refusing to commit non-test path: {p.test_file}. "
                "test_maintainer is hard-restricted to test files only."
            )

    # --- SEC-8: validate everything that reaches a git/gh argv. -------------
    diff_against = _validate_git_ref(diff_against)
    base = _validate_git_ref(base)
    short_sha = _short_sha(project_root)
    branch = branch_name_for(short_sha)
    title = pr_title if pr_title is not None else f"test(maintainer): {_summary_line(kept)}"
    title = _validate_pr_title(title)
    msg = commit_message or "test(maintainer): apply verified-green edits"

    body = render_pr_body(
        outcome,
        diff_against=diff_against,
        impacted=impacted,
        coverage_source=coverage_source,
        unclosed_gaps=unclosed_gaps,
    )

    def _record(tool: str, *args: str) -> None:
        if recorder is not None:
            recorder.append(_Cmd(tool=tool, args=tuple(args)))

    committed = tuple(str(p.test_file) for p in kept)

    if dry_run:
        _record("git", "checkout", "-b", branch)
        _record("git", "commit", "-m", msg)
        _record("git", "push", "-u", remote, branch)
        _record("gh", "pr", "create", "--base", base, "--head", branch, "--title", title)
        return PrResult(
            opened_pr=True,
            branch=branch,
            pr_url="",
            committed=committed,
            pr_body=body,
        )

    # --- 1. Branch. --------------------------------------------------------
    _record("git", "checkout", "-b", branch)
    co = _git(["checkout", "-b", branch], project_root)
    if co.returncode != 0:
        return PrResult(
            opened_pr=False,
            branch=branch,
            committed=(),
            pr_body=body,
            error=f"git checkout -b failed: {co.stderr.strip()}",
        )

    # --- 2. Stage ONLY the green edits (re-assert they are on disk). --------
    project_root_resolved = project_root.resolve()
    for p in kept:
        target = p.test_file if p.test_file.is_absolute() else project_root / p.test_file
        rel = (
            target.resolve().relative_to(project_root_resolved) if target.is_absolute() else target
        )
        _record("git", "add", str(rel))
        _git(["add", str(rel)], project_root)

    # --- 3. Commit. --------------------------------------------------------
    _record("git", "commit", "-m", msg)
    commit = _git(["commit", "-m", msg], project_root)
    if commit.returncode != 0:
        return PrResult(
            opened_pr=False,
            branch=branch,
            committed=(),
            pr_body=body,
            error=f"git commit failed: {commit.stderr.strip()}",
        )

    # --- 4. Push. ----------------------------------------------------------
    _record("git", "push", "-u", remote, branch)
    push = _git(["push", "-u", remote, branch], project_root)
    if push.returncode != 0:
        return PrResult(
            opened_pr=False,
            branch=branch,
            committed=committed,
            pr_body=body,
            error=f"git push failed: {push.stderr.strip()}",
        )

    # --- 5. gh pr create. --------------------------------------------------
    pr_url = _gh_pr_create(
        project_root,
        base=base,
        branch=branch,
        title=title,
        body=body,
        recorder=recorder,
    )
    return PrResult(
        opened_pr=True,
        branch=branch,
        pr_url=pr_url,
        committed=committed,
        pr_body=body,
    )


def _gh_pr_create(
    project_root: Path,
    *,
    base: str,
    branch: str,
    title: str,
    body: str,
    recorder: list[_Cmd] | None,
) -> str:
    """Run ``gh pr create`` with a body file; return the PR URL (or "")."""
    body_file = project_root / ".git" / "FORGE_TM_PR_BODY.md"
    body_file.parent.mkdir(parents=True, exist_ok=True)
    body_file.write_text(body, encoding="utf-8")
    args = [
        "pr",
        "create",
        "--base",
        base,
        "--head",
        branch,
        "--title",
        title,
        "--body-file",
        str(body_file),
    ]
    if recorder is not None:
        recorder.append(_Cmd(tool="gh", args=tuple(args)))
    result = _gh(args, project_root)
    return result.stdout.strip() if result.returncode == 0 else ""


def comment_report(
    project_root: Path,
    body: str,
    *,
    pr_ref: str | None = None,
    recorder: list[_Cmd] | None = None,
    dry_run: bool = False,
) -> bool:
    """Post *body* as a ``gh pr comment`` (the zero-green CI report path).

    Returns True when the comment was posted (or recorded under *dry_run*).
    Used in CI when there are no green edits to open a PR for, but a current PR
    exists — the maintainer leaves its report as an audit-trail comment.
    """
    args = ["pr", "comment", "--body", body]
    if pr_ref is not None:
        args.append(_validate_git_ref(pr_ref))
    if recorder is not None:
        recorder.append(_Cmd(tool="gh", args=tuple(args)))
    if dry_run:
        return True
    result = _gh(args, project_root)
    return result.returncode == 0


__all__ = [
    "PrInputError",
    "PrResult",
    "branch_name_for",
    "comment_report",
    "open_pr_for_green_edits",
    "render_pr_body",
]

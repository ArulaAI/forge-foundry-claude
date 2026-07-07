"""C7 PR prep — ``gh pr create`` / ``gh pr comment`` (spec §4).

One PR for the whole tree onto ``foundry/<root-issue-slug>``, opened **only if
the tree-gate is ``pass``** (the "fully clean" condition). When not clean, no PR
is opened — the caller surfaces the WorkflowReport instead; ``--force-pr`` opens
a **draft** PR with the findings attached. Merge stays advisory — this module
**never auto-merges** and **never pushes/merges the base branch**. It does not push
by default; with ``--push`` it pushes the **feature branch only**
(``foundry/<slug>``) so ``gh pr create`` can open the PR. Without ``--push``, if the
head branch is not already on the remote it surfaces the exact push + create
commands instead of failing (#1107).

SEC-8: every value reaching a ``gh`` argv is regex-validated CLI-side with the
first character constrained so a leading ``-`` cannot be parsed as a flag;
``shell=True`` is never used; argv is always a list; an explicit ``timeout`` is
always passed. Pattern mirrors ``test_maintainer/shared/pr.py``.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from primitives.work_orchestrator.shared.gating import should_open_pr

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.work_orchestrator.shared.gating import TreeGate

# ---------------------------------------------------------------------------
# SEC-8 validators — first char excludes ``-`` so a value can't be a flag.
# ---------------------------------------------------------------------------

#: A branch name: first char alphanumeric; body admits the branch alphabet
#: (``foundry/<slug>`` is built from a sanitized slug, so this is belt-and-braces).
_VALID_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: A PR title: first char alphanumeric; body admits ordinary text but NO control
#: chars, ``$`` or backtick (no newline / shell-expansion vector into argv).
_VALID_TITLE = re.compile(r"^[A-Za-z0-9][^\x00-\x1f$`]*$")

#: Default timeout (seconds) for a ``gh``/``git`` subprocess call (#1008).
_SUBPROCESS_TIMEOUT = 60

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrOutcome:
    """The result of a PR-prep attempt.

    ``opened`` is ``True`` only when ``gh pr create`` ran. ``draft`` records
    whether it was a forced draft. ``url`` is the created PR URL (or ``""``).
    ``reason`` explains a no-PR outcome (e.g. "tree not clean").
    """

    opened: bool
    draft: bool
    url: str
    reason: str


def _validate(value: str, pattern: re.Pattern[str], field: str) -> None:
    if not pattern.match(value):
        msg = f"invalid {field} {value!r}: must match {pattern.pattern}"
        raise ValueError(msg)


#: SEC-8: a GitHub issue ref reaching the `gh issue view` argv. First char
#: excludes `-` so it can never be parsed as a flag; body admits the ref alphabet
#: (URL / owner/repo#N / #N / N). Mirrors the CLI's _VALID_ISSUE_REF.
_VALID_ISSUE_REF = re.compile(r"^[A-Za-z0-9#][A-Za-z0-9._/:#-]*$")

#: The documented `owner/repo#N` form. `gh issue view` does NOT accept it directly
#: (it wants `view N --repo owner/repo`), so it is translated (#1097).
_OWNER_REPO_ISSUE = re.compile(r"^(?P<repo>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(?P<num>\d+)$")


def _issue_view_args(issue_ref: str) -> list[str]:
    """Build the ``gh issue view`` argv for *issue_ref* (#1097).

    A bare ``#N`` / ``N`` and a full issue URL are accepted by ``gh issue view``
    verbatim, but the documented ``owner/repo#N`` form is not — it must become
    ``view <N> --repo owner/repo``. *issue_ref* has already passed SEC-8 validation,
    so both split components are drawn from the validated alphabet.
    """
    tail = ["--json", "title,body", "--jq", "{title,body}"]
    m = _OWNER_REPO_ISSUE.match(issue_ref)
    if m:
        return ["issue", "view", m.group("num"), "--repo", m.group("repo"), *tail]
    return ["issue", "view", issue_ref, *tail]


def _run(binary: str, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``<binary> <args>`` (list argv, ``shell=False``, explicit timeout) (#1008).

    The single subprocess sink for C7: list-form argv (never a shell string),
    ``check=False`` (callers inspect ``returncode``), output captured, with a
    bounded ``_SUBPROCESS_TIMEOUT``. *binary* is a fixed literal at every call
    site (``"gh"`` / ``"git"``), never user input — so the argv head can't be
    attacker-controlled; SEC-8 validation guards the *args* at the call sites.
    """
    return subprocess.run(  # noqa: S603
        [binary, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT,
    )


def _gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` — thin wrapper over :func:`_run`. No raise on non-zero."""
    return _run("gh", args, cwd)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` — thin wrapper over :func:`_run`. No raise on non-zero.

    C7 uses git ONLY to inspect/create the build branch (issue/§2 `branch` stage).
    It never pushes — the PR is raised via ``gh pr create`` (spec §4).
    """
    return _run("git", args, cwd)


@dataclass(frozen=True)
class IssueDetails:
    """The title + body fetched for the root issue (the §3.1 `issue` stage)."""

    title: str
    body: str


def fetch_issue(issue_ref: str, project_root: Path, *, run_gh: bool = True) -> IssueDetails | None:
    """Fetch the issue title + body via ``gh issue view`` (Mode-1 `issue` stage).

    SEC-8: *issue_ref* is regex-validated (first char excludes ``-``) before it
    reaches the ``gh`` argv. Returns ``None`` when the ref is unresolvable (caller
    maps that to exit 2) or when ``run_gh=False`` (dry-run / tests). The fetched
    title/body seed the root node's spec context so the root spec stage no longer
    runs against an empty seed (#985).
    """
    _validate(issue_ref, _VALID_ISSUE_REF, "issue_ref")
    if not run_gh:
        return None
    result = _gh(_issue_view_args(issue_ref), project_root)
    if result.returncode != 0:
        _logger.warning(
            "gh issue view %s failed (exit %d): %s",
            issue_ref,
            result.returncode,
            result.stderr.strip(),
        )
        return None
    try:
        import json  # noqa: PLC0415 — stdlib, lazy to keep module import light

        data = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    return IssueDetails(title=str(data.get("title", "")), body=str(data.get("body", "")))


def ensure_branch(
    root_slug: str,
    project_root: Path,
    *,
    run_git: bool = True,
) -> PrOutcome:
    """Create + check out the single build branch ``foundry/<root_slug>`` (Mode-1).

    Refuses a dirty working tree (the spec's "refuses dirty tree" guard) and
    **never** ``git push``. Returns a :class:`PrOutcome`-shaped result reusing
    ``opened`` to mean "branch ready" and ``reason`` for any failure. ``run_git=
    False`` validates + skips the git calls (dry-run / tests).
    """
    branch = f"foundry/{root_slug}"
    _validate(branch, _VALID_BRANCH, "branch")
    if not run_git:
        return PrOutcome(opened=True, draft=False, url=branch, reason="dry-run (git not invoked)")

    status = _git(["status", "--porcelain"], project_root)
    if status.returncode != 0:
        return PrOutcome(
            opened=False, draft=False, url="", reason=f"git status failed: {status.stderr.strip()}"
        )
    if status.stdout.strip():
        return PrOutcome(
            opened=False,
            draft=False,
            url="",
            reason="refusing to branch: the working tree is dirty (commit or stash first)",
        )
    # Idempotent on resume (#1006): if the build branch already exists (a prior
    # run created it), check it out WITHOUT -b; only create it when absent. A bare
    # `checkout -b` on an existing branch fails, and the old code treated that
    # failure as non-fatal — so a re-entered extract could silently proceed on the
    # wrong branch. Now an unexpected git failure is surfaced (the caller hard-stops).
    exists = _git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], project_root)
    if exists.returncode == 0:
        co = _git(["checkout", branch], project_root)
        verb = "checkout"
    else:
        co = _git(["checkout", "-b", branch], project_root)
        verb = "checkout -b"
    if co.returncode != 0:
        return PrOutcome(
            opened=False, draft=False, url="", reason=f"git {verb} failed: {co.stderr.strip()}"
        )
    return PrOutcome(opened=True, draft=False, url=branch, reason="")


def _remote_branch_exists(branch: str, project_root: Path) -> bool:
    """True if ``origin/<branch>`` exists on the remote (``git ls-remote``)."""
    result = _git(["ls-remote", "--heads", "origin", branch], project_root)
    return result.returncode == 0 and bool(result.stdout.strip())


def prepare_pr(
    *,
    gate: TreeGate,
    root_slug: str,
    title: str,
    body: str,
    project_root: Path,
    base: str = "main",
    force_pr: bool = False,
    push: bool = False,
    run_gh: bool = True,
) -> PrOutcome:
    """Open one PR for the tree onto ``foundry/<root_slug>`` — only if fully clean.

    The "fully clean" gate (spec §4): when :func:`gating.should_open_pr` is
    ``False`` and ``force_pr`` is ``False``, **no PR is opened** — returns
    ``opened=False`` with a reason. With ``force_pr`` a **draft** PR is opened
    and the findings travel in the body.

    Push policy (#1107): ``gh pr create`` requires the head branch on the remote,
    but C7 does **not** push by default (safety invariant — the autonomous engine
    never does outward-facing pushes). So:

    * ``push=True`` — push the **feature branch only** (``git push -u origin
      foundry/<slug>``) immediately before ``gh pr create``. Never pushes/merges
      the base branch.
    * ``push=False`` (default) — if the head branch is not already on the remote,
      **do not fail** with a cryptic ``gh`` error: return ``opened=False`` with a
      reason carrying the exact ``git push`` + ``gh pr create`` commands for the
      operator to run. If the branch *is* already on the remote, proceed.

    ``run_gh=False`` validates inputs + computes the decision without invoking
    ``gh``/``git`` (used by ``--dry-run`` and by tests) — it returns the would-be
    outcome with ``url=""``.
    """
    branch = f"foundry/{root_slug}"
    _validate(branch, _VALID_BRANCH, "branch")
    _validate(base, _VALID_BRANCH, "base")
    _validate(title, _VALID_TITLE, "title")

    clean = should_open_pr(gate)
    if not clean and not force_pr:
        return PrOutcome(
            opened=False,
            draft=False,
            url="",
            reason=f"tree-gate is {gate.recommendation!r} (not clean); no PR opened",
        )

    draft = not clean  # a forced PR over a non-clean tree is always a draft
    if not run_gh:
        return PrOutcome(opened=True, draft=draft, url="", reason="dry-run (gh not invoked)")

    # Ensure the head branch is on the remote (gh pr create needs it) (#1107).
    if push:
        pushed = _git(["push", "-u", "origin", branch], project_root)
        if pushed.returncode != 0:
            return PrOutcome(
                opened=False,
                draft=draft,
                url="",
                reason=(
                    f"git push of {branch} failed "
                    f"(exit {pushed.returncode}): {pushed.stderr.strip()}"
                ),
            )
    elif not _remote_branch_exists(branch, project_root):
        return PrOutcome(
            opened=False,
            draft=draft,
            url="",
            reason=(
                f"head branch {branch!r} is not on the remote and --push was not set "
                "(C7 does not push by default). Run:\n"
                f"  git push -u origin {branch}\n"
                f"  gh pr create --base {base} --head {branch} "
                "--title <title> --body <report>\n"
                "or re-run with --push to have the orchestrator push + open the PR."
            ),
        )

    # SEC-8 note (#992): branch/base/title/pr_ref are first-char-constrained regex
    # validated above; `body` is the one value passed unvalidated. That is safe by
    # construction here — argv is a list with shell=False (no shell metachar
    # interpretation) and `--body` always immediately precedes the body token, so
    # `gh` consumes it as that flag's value regardless of its content (a body line
    # that looks like `--flag` is not re-parsed as an option). The body carries
    # model-generated Markdown that legitimately contains arbitrary text, so it is
    # intentionally not regex-constrained.
    args = [
        "pr",
        "create",
        "--base",
        base,
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    if draft:
        args.append("--draft")
    result = _gh(args, project_root)
    if result.returncode != 0:
        return PrOutcome(
            opened=False,
            draft=draft,
            url="",
            reason=f"gh pr create failed (exit {result.returncode}): {result.stderr.strip()}",
        )
    return PrOutcome(opened=True, draft=draft, url=result.stdout.strip(), reason="")


def comment_report(
    *,
    pr_ref: str,
    body: str,
    project_root: Path,
    run_gh: bool = True,
) -> bool:
    """Attach a structured report to a PR via ``gh pr comment`` (spec §4).

    ``pr_ref`` is a PR number or URL. Returns ``True`` on success. ``run_gh=False``
    validates + skips the call (dry-run/tests). Shell-safe, explicit timeout. On a
    non-zero ``gh`` exit the failure is logged at WARNING with the captured stderr
    (#992) — the report attachment is best-effort, but it is no longer silently
    dropped.
    """
    _validate(pr_ref, re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$"), "pr_ref")
    if not run_gh:
        return True
    result = _gh(["pr", "comment", pr_ref, "--body", body], project_root)
    if result.returncode != 0:
        _logger.warning(
            "gh pr comment on %s failed (exit %d): %s",
            pr_ref,
            result.returncode,
            result.stderr.strip(),
        )
        return False
    return True

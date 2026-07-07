"""propose_test_edits() — S2 pipeline composer entrypoint.

End-to-end orchestration: git diff → depgraph → impact classify →
LLM propose → C10 audit gate.

Regex-mode in v0.3.0 detects affected tests but does not produce edit
proposals (full regex-mode proposing is deferred to v0.4.0).
"""

from __future__ import annotations

import json as json_mod
import subprocess
import sys
from typing import TYPE_CHECKING, NamedTuple

from primitives._sdet.config import SdetConfig
from primitives.test_maintainer.shared.edit_proposal import (
    JSON_SCHEMA_VERSION,
    EditProposal,
    ImpactSummary,
    render_markdown_report,
    to_json,
)

if TYPE_CHECKING:
    from pathlib import Path

    from primitives._sdet.stacks.contract import TestStackAdapter, TestSubset
    from primitives.test_maintainer.shared.impact import ImpactReport
    from primitives.test_maintainer.shared.verify import ReproposeFn, VerifyOutcome


#: Default per-run cap on concurrently in-flight edit-proposal judgments.
_DEFAULT_CONCURRENCY = 5

#: Emitted to stderr when regex mode short-circuits the closed loop.
_REGEX_MODE_NOTICE = (
    "S2 NOTICE: running in regex mode — proposing deferred; "
    "set s2.mode: llm in .foundry/sdet.yaml to enable the closed loop "
    "(propose → audit → green-gate → PR)."
)

#: Emitted to stderr when the 3-dot diff resolves to zero changed files.
_ZERO_DIFF_WARNING = (
    "S2 WARNING: diff against {ref!r} resolved to 0 changed files — "
    "uncommitted/intent-to-add files are not seen by the 3-dot (merge-base) "
    "diff; commit your work first."
)


class _WorkUnit(NamedTuple):
    """One impacted (source, test) pair awaiting an edit-proposal judgment."""

    impact: ImpactReport
    source_before: str
    source_after: str
    test_content: str


class MaintainerBudgetExceededError(RuntimeError):
    """Raised when --max-files cap is exceeded by the diff."""


_TEST_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java"}


def _is_test_path(p: Path) -> bool:
    name = p.name
    if name.startswith("test_") or "test_" in name:
        return True
    if name.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".test.mjs", ".test.cjs")):
        return True
    if name.endswith((".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx")):
        return True
    if "tests" in p.parts or "__tests__" in p.parts or "test" in p.parts:
        return True
    return bool(name.endswith(("Test.java", "Tests.java", "IT.java")))


def _git_changed_files(project_root: Path, diff_against: str) -> list[Path]:
    result = subprocess.run(  # noqa: S603
        ["git", "diff", "--name-only", f"{diff_against}...HEAD"],  # noqa: S607
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [project_root / line.strip() for line in result.stdout.splitlines() if line.strip()]


def diff_impact_counts(project_root: Path, diff_against: str) -> tuple[int, int]:
    """Return ``(changed_files, source_changed)`` for the diff (#1108).

    ``changed_files`` is every path in the 3-dot diff; ``source_changed`` is the
    subset that is testable source (a code extension that is **not** a test file).
    Used to populate the impact summary in the deterministic / regex-mode paths,
    which previously reported a hard-coded ``0/0/0`` regardless of the real diff.
    """
    all_changed = _git_changed_files(project_root, diff_against)
    source_changed = [
        f for f in all_changed if f.suffix in _TEST_EXTENSIONS and not _is_test_path(f)
    ]
    return len(all_changed), len(source_changed)


def _git_show_before(project_root: Path, diff_against: str, file: Path) -> str:
    rel = file.relative_to(project_root)
    result = subprocess.run(  # noqa: S603
        ["git", "show", f"{diff_against}:{rel}"],  # noqa: S607
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def propose_test_edits(
    project_root: Path,
    *,
    diff_against: str,
    config: SdetConfig | None = None,
) -> tuple[list[EditProposal], list[tuple[EditProposal, list[str]]]]:
    """Run the deterministic S2 pre-flight: diff → file-count budget check.

    The edit-proposal step that needs the model is now driven by the
    skill-first ``--extract-only`` / ``--continue`` protocol (the SKILL.md body
    judges each impacted test in-session). This function only performs the
    deterministic preamble: it still enforces the ``--max-files`` budget and
    returns empty proposal lists. ``regex`` / disabled modes likewise produce
    no proposals.

    When ``s2.mode == "regex"`` a NOTICE is emitted to stderr explaining that
    the closed loop is deferred and how to enable it.  When the 3-dot
    ``git diff ... HEAD`` resolves to zero changed files a WARNING is emitted
    to stderr — uncommitted / intent-to-add files are invisible to the
    merge-base form.

    Args:
        project_root: Project root.
        diff_against: Git ref (e.g., "HEAD~1", "main") to diff against.
        config: SdetConfig; defaults if None.

    Returns:
        ``([], [])`` — proposals are produced via the skill-first path.

    Raises:
        MaintainerBudgetExceededError if changed-file count >
            config.s2.max_files_per_run.
    """
    if config is None:
        config = SdetConfig()
    if not config.s2.enabled:
        return [], []

    if config.s2.mode == "regex":
        print(_REGEX_MODE_NOTICE, file=sys.stderr)

    project_root = project_root.resolve()
    all_changed = _git_changed_files(project_root, diff_against)

    if not all_changed:
        print(
            _ZERO_DIFF_WARNING.format(ref=diff_against),
            file=sys.stderr,
        )

    # Filter to source files only (not test files — tests are output, not input)
    source_changed = [
        f for f in all_changed if f.suffix in _TEST_EXTENSIONS and not _is_test_path(f)
    ]

    if len(source_changed) > config.s2.max_files_per_run:
        raise MaintainerBudgetExceededError(
            f"Changed file count {len(source_changed)} exceeds "
            f"max_files_per_run={config.s2.max_files_per_run}. "
            "Narrow your diff or raise the cap in .foundry/sdet.yaml."
        )

    # No model proposals here — the skill-first path drives the judgments.
    return [], []


def write_dry_run_report(
    project_root: Path,
    accepted: list[EditProposal],
    dropped: list[tuple[EditProposal, list[str]]],
    *,
    output_path: Path | None = None,
    format_: str = "markdown",
    impact_summary: ImpactSummary | None = None,
) -> Path:
    """Write a dry-run report file. Returns the written path.

    Default output_path = project_root/.foundry/proposed-test-edits.md.
    format_ = 'markdown' (default) or 'json'.

    When *impact_summary* is provided it is included in the report so an
    operator can distinguish "no impact found" from "engine skipped (wrong
    mode / empty diff)."  The summary is included for BOTH empty and
    non-empty proposal lists.

    The report shows:
        - Summary line: N accepted; A updates, B adds, C deletes; D dropped (FM5).
        - Per-proposal sections (markdown render or JSON wrapper).
        - Dropped section listing each dropped proposal + its warnings.
        - Impact summary section (always present when *impact_summary* given).
    """
    target = output_path or (project_root / ".foundry" / "proposed-test-edits.md")
    target.parent.mkdir(parents=True, exist_ok=True)

    if format_ == "json":
        accepted_json = json_mod.loads(to_json(accepted, impact_summary))
        payload: dict[str, object] = {
            "schema_version": JSON_SCHEMA_VERSION,
            "accepted": accepted_json["proposals"],
            "dropped": [
                {
                    "test_file": str(p.test_file),
                    "verdict": p.verdict.value,
                    "warnings": w,
                }
                for p, w in dropped
            ],
        }
        if impact_summary is not None:
            payload["impact_summary"] = impact_summary.to_dict()
        target.write_text(json_mod.dumps(payload, indent=2, ensure_ascii=False))
        return target

    # markdown
    sections: list[str] = [render_markdown_report(accepted, impact_summary)]
    if dropped:
        sections.append("")
        sections.append(f"## Dropped proposals ({len(dropped)})")
        sections.append("")
        for p, warnings in dropped:
            sections.append(f"- `{p.test_file}` (verdict: {p.verdict.value}) — dropped:")
            for w in warnings:
                sections.append(f"  - {w}")
            sections.append("")
    target.write_text("\n".join(sections))
    return target


def verify_phase(
    adapter: TestStackAdapter,
    project_root: Path,
    accepted: list[EditProposal],
    *,
    subset: TestSubset,
    run_dir: Path | None = None,
    config: SdetConfig | None = None,
    repropose: ReproposeFn | None = None,
    subset_for: dict[str, TestSubset] | None = None,
) -> VerifyOutcome:
    """Run the strict green-gate verify loop (the SECOND gate).

    The C10/C11 audit gate (``audit_gate.audit_proposals``) runs FIRST; only
    its surviving *accepted* proposals are passed here.  This phase writes each
    edit, runs the impacted *subset* via the stack adapter, keeps the
    verified-green edits, and reverts the rest into the needs-human list (design
    spec §6).  The bounded refine count comes from ``config.s2.max_refine``
    (``.foundry/sdet.yaml``); ``repropose`` is the injected in-session re-judge
    seam.

    When *run_dir* is given, ``verify-results.json`` is emitted under it.
    """
    from primitives.test_maintainer.shared.verify import (  # noqa: PLC0415
        verify_edits,
        write_verify_results,
    )

    if config is None:
        config = SdetConfig()

    outcome = verify_edits(
        adapter,
        project_root,
        accepted,
        max_refine=config.s2.max_refine,
        repropose=repropose,
        subset=subset,
        subset_for=subset_for,
    )
    if run_dir is not None:
        write_verify_results(run_dir, outcome)
    return outcome

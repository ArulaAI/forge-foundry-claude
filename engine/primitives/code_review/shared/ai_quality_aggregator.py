"""C4 /code-review — AI Quality Aggregator (sub-call rule §3.4).

Invokes C10 (audit_path) and C11 (audit_production_path).

Dedup discipline (C8 lesson applied to C4):
  For each C10/C11 finding at the same (file, line) as a C4 issue:
    - MOVE the finding to `deduplicated_findings` with a note
    - REMOVE it from c10_findings / c11_findings
    - The C4 issue remains canonical in `issues`
    - The finding is NOT double-counted

The shared report dataclasses (:class:`AIQualityReport`, :class:`DedupEntry`),
the C10/C11 invocation, and the summary string come from
``primitives._shared.ai_quality`` (#722); only the C4-specific dedup callable
lives here.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from primitives._shared.ai_quality import (
    AIQualityReport,
    DedupEntry,
    dedup_against_canonical,
    format_summary,
    run_c10_c11,
)

if TYPE_CHECKING:
    from primitives.code_review.shared.models import CodeReviewIssue
    from primitives.failure_mode_audit import Finding

__all__ = ["AIQualityReport", "DedupEntry", "aggregate_ai_quality"]


def _dedup_against_c4_issues(
    c10_findings: list[Finding],
    c11_findings: list[Finding],
    c4_issues: list[CodeReviewIssue],
) -> tuple[list[Finding], list[Finding], list[DedupEntry]]:
    """Apply dedup discipline: move C10/C11 findings that overlap C4 issues.

    For each C10/C11 finding, if a C4 issue exists at the same (file, line),
    the finding is MOVED to deduplicated_findings with a note. C4 dedups every
    FM mode (no mode filter).

    Args:
        c10_findings: C10 audit findings.
        c11_findings: C11 audit findings.
        c4_issues: C4 code review issues (canonical).

    Returns:
        (remaining_c10, remaining_c11, dedup_entries)
    """
    # Build set of (file, line) and (basename, line) tuples from C4 issues for
    # fast lookup — both forms handle relative vs absolute path mismatches.
    c4_keys: set[tuple[str, int]] = set()
    for issue in c4_issues:
        c4_keys.add((issue.file, issue.line))
        c4_keys.add((Path(issue.file).name, issue.line))

    return dedup_against_canonical(
        c10_findings,
        c11_findings,
        c4_keys,
        c10_modes=None,
        c11_modes=None,
        note=lambda _tag, f: f"(duplicate of C4 finding at {f.file}:{f.line})",
        log_line=lambda tag, f: (
            f"[code-review] dedup: {tag} finding at {f.file}:{f.line} "
            f"overlaps C4 issue; moved to deduplicated_findings"
        ),
    )


def aggregate_ai_quality(
    code_path: Path,
    c4_issues: list[CodeReviewIssue] | None = None,
) -> AIQualityReport:
    """Invoke C10 and C11 on *code_path* and return bundled findings with dedup.

    Args:
        code_path: Path to the code directory or file to audit.
        c4_issues: C4 issues to dedup against (canonical). If None, no dedup.

    Returns:
        AIQualityReport with c10_findings, c11_findings, deduplicated_findings,
        and a summary string.
    """
    c10_findings, c11_findings = run_c10_c11(code_path)

    dedup_entries: list[DedupEntry] = []
    if c4_issues:
        c10_findings, c11_findings, dedup_entries = _dedup_against_c4_issues(
            c10_findings, c11_findings, c4_issues
        )

    n_dedup = len(dedup_entries)
    summary = format_summary(
        c10_findings,
        c11_findings,
        n_dedup=n_dedup,
        extra_clauses=[f"Deduplicated (moved to ai_quality.deduplicated_findings): {n_dedup}."],
    )

    return AIQualityReport(
        c10_findings=c10_findings,
        c11_findings=c11_findings,
        deduplicated_findings=dedup_entries,
        summary=summary,
    )

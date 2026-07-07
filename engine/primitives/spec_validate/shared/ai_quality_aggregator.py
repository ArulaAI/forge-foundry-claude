"""C5 /spec-validate — AI Quality Aggregator (sub-call rule §3.4).

Invokes C10 (audit_path) and C11 (audit_production_path).

Dedup discipline (C8 lesson applied to C5):
  For each C10/C11 finding at the same (file, line) as a C5 unsatisfied/partial trace:
    - MOVE the finding to `deduplicated_findings` with a note
    - REMOVE it from c10_findings / c11_findings
    - The C5 trace remains canonical
    - The finding is NOT double-counted

C10 FM2 integration: code elements from unsatisfied traces represent
LLM-hallucinated code references. FM2 (context-window blindness) is the
detector most likely to overlap with unsatisfied traces — the LLM proposed
a cross-file reference that doesn't exist (both C5 and FM2 flag the same gap).

The shared report dataclasses (:class:`AIQualityReport`, :class:`DedupEntry`),
the C10/C11 invocation, and the summary string come from
``primitives._shared.ai_quality`` (#722); only the C5-specific dedup callable
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
    from primitives.failure_mode_audit import Finding
    from primitives.spec_validate.shared.models import AcceptanceTrace

__all__ = ["AIQualityReport", "DedupEntry", "aggregate_ai_quality"]


def _dedup_against_c5_traces(
    c10_findings: list[Finding],
    c11_findings: list[Finding],
    traces: list[AcceptanceTrace],
) -> tuple[list[Finding], list[Finding], list[DedupEntry]]:
    """Apply dedup discipline: move C10/C11 findings that overlap C5 traces.

    For each C10/C11 finding, if a C5 trace references a code element at the
    same (file, line), the finding is MOVED to deduplicated_findings with a note.
    C5 dedups every FM mode (no mode filter).

    Args:
        c10_findings: C10 audit findings.
        c11_findings: C11 audit findings.
        traces: C5 acceptance traces (canonical).

    Returns:
        (remaining_c10, remaining_c11, dedup_entries)
    """
    # Build set of (file, line) and (basename, line) from all code elements in
    # C5 traces — both forms handle relative vs absolute path mismatches.
    c5_keys: set[tuple[str, int]] = set()
    for trace in traces:
        for elem in trace.code_elements:
            c5_keys.add((elem.file, elem.line))
            c5_keys.add((Path(elem.file).name, elem.line))

    return dedup_against_canonical(
        c10_findings,
        c11_findings,
        c5_keys,
        c10_modes=None,
        c11_modes=None,
        note=lambda _tag, f: f"(duplicate of C5 trace at {f.file}:{f.line})",
        log_line=lambda tag, f: (
            f"[spec-validate] dedup: {tag} finding at {f.file}:{f.line} "
            f"overlaps C5 trace; moved to deduplicated_findings"
        ),
    )


def aggregate_ai_quality(
    code_path: Path,
    traces: list[AcceptanceTrace] | None = None,
) -> AIQualityReport:
    """Invoke C10 and C11 on *code_path* and return bundled findings with dedup.

    Args:
        code_path: Path to the code directory or file to audit.
        traces: C5 acceptance traces to dedup against (canonical). If None, no dedup.

    Returns:
        AIQualityReport with c10_findings, c11_findings, deduplicated_findings,
        and a summary string.
    """
    c10_findings, c11_findings = run_c10_c11(code_path)

    dedup_entries: list[DedupEntry] = []
    if traces:
        c10_findings, c11_findings, dedup_entries = _dedup_against_c5_traces(
            c10_findings, c11_findings, traces
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

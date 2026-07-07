"""S11 /security-review — AI Quality Aggregator (sub-call rule §3.4).

Invokes C10 (audit_path) and C11 (audit_production_path).

Dedup discipline (C8 lesson applied to S11):
  For each C10/C11 finding at the same (file, line) as an S11 finding:
    - MOVE the finding to `deduplicated_findings` with a note
    - REMOVE it from c10_findings / c11_findings
    - The S11 finding remains canonical
    - The finding is NOT double-counted

Specifically checks C10 FM2 (hallucinated APIs), FM4 (insecure defaults),
C11 FM7 (overgeneralization), FM8 (compliance blindness) for overlap.

The shared report dataclasses (:class:`AIQualityReport`, :class:`DedupEntry`),
the C10/C11 invocation, and the summary string come from
``primitives._shared.ai_quality`` (#722); only the S11-specific dedup callable
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
    from primitives.security_review.shared.models import SecurityFinding

__all__ = ["AIQualityReport", "DedupEntry", "aggregate_ai_quality"]

# C10 FM modes relevant for S11 dedup (FM2 hallucinated APIs, FM4 insecure defaults)
_C10_DEDUP_MODES: frozenset[str] = frozenset({"fm2", "fm4"})

# C11 FM modes relevant for S11 dedup (FM7 overgeneralization, FM8 compliance blindness)
_C11_DEDUP_MODES: frozenset[str] = frozenset({"fm7", "fm8"})


def _dedup_against_s11_findings(
    c10_findings: list[Finding],
    c11_findings: list[Finding],
    s11_findings: list[SecurityFinding],
) -> tuple[list[Finding], list[Finding], list[DedupEntry]]:
    """Apply dedup discipline: move C10/C11 findings that overlap S11 findings.

    For each C10/C11 finding at the same (file, line), if an S11 finding exists
    at that location, the C10/C11 finding is MOVED to deduplicated_findings.

    Specifically deduplicates:
      - C10 FM2 (hallucinated APIs) and FM4 (insecure defaults)
      - C11 FM7 (overgeneralization) and FM8 (compliance blindness)

    Args:
        c10_findings: C10 audit findings.
        c11_findings: C11 audit findings.
        s11_findings: S11 security review findings (canonical).

    Returns:
        (remaining_c10, remaining_c11, dedup_entries)
    """
    # Build set of (file, line) and (basename, line) tuples from S11 findings
    # for fast lookup — both forms handle relative vs absolute path mismatches.
    s11_keys: set[tuple[str, int]] = set()
    for s11_f in s11_findings:
        s11_keys.add((s11_f.file, s11_f.line))
        s11_keys.add((Path(s11_f.file).name, s11_f.line))

    return dedup_against_canonical(
        c10_findings,
        c11_findings,
        s11_keys,
        c10_modes=_C10_DEDUP_MODES,
        c11_modes=_C11_DEDUP_MODES,
        note=lambda tag, f: (
            f"duplicate of S11 finding at {f.file}:{f.line} "
            f"({tag} {f.mode.upper()} overlaps S11 security finding)"
        ),
        log_line=lambda tag, f: (
            f"[security-review] dedup: {tag} {f.mode.upper()} finding at "
            f"{f.file}:{f.line} overlaps S11 finding; "
            f"moved to deduplicated_findings"
        ),
    )


def aggregate_ai_quality(
    code_path: Path,
    s11_findings: list[SecurityFinding] | None = None,
) -> AIQualityReport:
    """Invoke C10 and C11 on *code_path* and return bundled findings with dedup.

    Args:
        code_path: Path to the code directory or file to audit.
        s11_findings: S11 findings to dedup against (canonical). If None, no dedup.

    Returns:
        AIQualityReport with c10_findings, c11_findings, deduplicated_findings,
        and a summary string.
    """
    c10_findings, c11_findings = run_c10_c11(code_path)

    dedup_entries: list[DedupEntry] = []
    if s11_findings:
        c10_findings, c11_findings, dedup_entries = _dedup_against_s11_findings(
            c10_findings, c11_findings, s11_findings
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

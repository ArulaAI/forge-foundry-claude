"""S40 /api-contract-review — AI Quality Aggregator (sub-call rule §3.4).

Invokes C10 (audit_path) and C11 (audit_production_path).

Dedup discipline (C8 lesson applied to S40):
  For each C10/C11 finding at the same (file, line) as an S40 finding:
    - MOVE the finding to `deduplicated_findings` with a note
    - REMOVE it from c10_findings / c11_findings
    - The S40 finding remains canonical
    - The finding is NOT double-counted

Specifically checks C11 FM6 (stale API patterns — OpenAPI 2.0 / Swagger),
FM7 (overgeneralization in auth schemes) for overlap with S40 findings.

The shared report dataclasses (:class:`AIQualityReport`, :class:`DedupEntry`),
the C10/C11 invocation, and the summary string come from
``primitives._shared.ai_quality`` (#722); only the S40-specific dedup callable
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
    from primitives.api_contract_review.shared.models import APIFinding
    from primitives.failure_mode_audit import Finding

__all__ = ["AIQualityReport", "DedupEntry", "aggregate_ai_quality"]

# C11 FM modes relevant for S40 dedup: FM6 stale patterns, FM7 overgeneralization
_C11_DEDUP_MODES: frozenset[str] = frozenset({"fm6", "fm7"})


def _dedup_against_s40_findings(
    c10_findings: list[Finding],
    c11_findings: list[Finding],
    s40_findings: list[APIFinding],
) -> tuple[list[Finding], list[Finding], list[DedupEntry]]:
    """Apply dedup discipline: move C10/C11 findings that overlap S40 findings.

    For each C10/C11 finding at the same (file, line), if an S40 finding exists
    at that location, the C10/C11 finding is MOVED to deduplicated_findings.

    Specifically deduplicates:
      - C11 FM6 (stale API patterns — OpenAPI 2.0 / Swagger)
      - C11 FM7 (overgeneralization in auth schemes)

    C10 findings are not specifically deduped against S40 (no overlap modes), so
    the C10 mode allow-list is empty and every C10 finding passes through.

    Args:
        c10_findings: C10 audit findings.
        c11_findings: C11 audit findings.
        s40_findings: S40 lens review findings (canonical).

    Returns:
        (remaining_c10, remaining_c11, dedup_entries)
    """
    # Build set of (file, line) and (basename, line) tuples from S40 findings
    # for fast lookup — both forms handle relative vs absolute path mismatches.
    s40_keys: set[tuple[str, int]] = set()
    for s40_f in s40_findings:
        s40_keys.add((s40_f.file, s40_f.line))
        s40_keys.add((Path(s40_f.file).name, s40_f.line))

    return dedup_against_canonical(
        c10_findings,
        c11_findings,
        s40_keys,
        c10_modes=frozenset(),  # C10 is never deduped against S40.
        c11_modes=_C11_DEDUP_MODES,
        note=lambda tag, f: (
            f"duplicate of S40 finding at {f.file}:{f.line} "
            f"({tag} {f.mode.upper()} overlaps S40 api-contract-review finding)"
        ),
        log_line=lambda tag, f: (
            f"[api-contract-review] dedup: {tag} {f.mode.upper()} finding at "
            f"{f.file}:{f.line} overlaps S40 finding; "
            f"moved to deduplicated_findings"
        ),
    )


def aggregate_ai_quality(
    contract_path: Path,
    s40_findings: list[APIFinding] | None = None,
) -> AIQualityReport:
    """Invoke C10 and C11 on *contract_path* and return bundled findings with dedup.

    Args:
        contract_path: Path to the contract file or directory to audit.
        s40_findings: S40 findings to dedup against (canonical). If None, no dedup.

    Returns:
        AIQualityReport with c10_findings, c11_findings, deduplicated_findings,
        and a summary string.
    """
    c10_findings, c11_findings = run_c10_c11(contract_path)

    dedup_entries: list[DedupEntry] = []
    if s40_findings:
        c10_findings, c11_findings, dedup_entries = _dedup_against_s40_findings(
            c10_findings, c11_findings, s40_findings
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

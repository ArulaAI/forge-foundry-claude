"""S70 /compliance-map — AI Quality Aggregator (sub-call rule §3.4).

Invokes C10 (audit_path) and C11 (audit_production_path) when a code path
is provided. Bundles findings into an AIQualityReport.

The report dataclass, the C10/C11 invocation, and the summary string come from
``primitives._shared.ai_quality`` (#722); S70 runs no dedup, so this module is a
thin wrapper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives._shared.ai_quality import (
    AIQualityReport,
    format_summary,
    run_c10_c11,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["AIQualityReport", "aggregate_ai_quality"]


def aggregate_ai_quality(
    code_path: Path,
) -> AIQualityReport:
    """Invoke C10 and C11 on *code_path* and return bundled findings.

    Args:
        code_path: Path to the code directory or file to audit.

    Returns:
        AIQualityReport with c10_findings, c11_findings, and a summary string.
    """
    c10_findings, c11_findings = run_c10_c11(code_path)
    summary = format_summary(c10_findings, c11_findings)
    return AIQualityReport(
        c10_findings=c10_findings,
        c11_findings=c11_findings,
        summary=summary,
    )

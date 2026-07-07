"""S60 /data-flow-map — AI Quality Aggregator (sub-call rule §3.4).

Invokes C10 (audit_path) and C11 (audit_production_path).
C11 FM8 (compliance blindness) findings are folded into the DataFlowReport's
retention/sinks lists with source="c11_fm8", and REMOVED from c11_findings
(C8 dedup lesson applied proactively) by the orchestrator.

The shared report dataclass, the C10/C11 invocation, and the summary string
come from ``primitives._shared.ai_quality`` (#722); the S60-specific
``fm8_findings`` view is added by the local subclass below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from primitives._shared.ai_quality import AIQualityReport as _BaseAIQualityReport
from primitives._shared.ai_quality import format_summary, run_c10_c11

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.failure_mode_audit import Finding

__all__ = ["AIQualityReport", "aggregate_ai_quality"]


@dataclass
class AIQualityReport(_BaseAIQualityReport):
    """S60 report — adds an FM8 (compliance blindness) view over c11_findings."""

    @property
    def fm8_findings(self) -> list[Finding]:
        """FM8 (compliance blindness) findings from C11."""
        return [f for f in self.c11_findings if f.mode == "fm8"]


def aggregate_ai_quality(
    code_path: Path,
) -> AIQualityReport:
    """Invoke C10 and C11 on *code_path* and return bundled findings.

    C11 FM8 findings are included in c11_findings. The caller is responsible
    for folding FM8 findings into retention/sinks with source="c11_fm8" AND
    removing them from c11_findings (C8 dedup lesson).

    Args:
        code_path: Path to the code directory or file to audit.

    Returns:
        AIQualityReport with c10_findings, c11_findings, and a summary string.
    """
    c10_findings, c11_findings = run_c10_c11(code_path)

    fm8_count = sum(1 for f in c11_findings if f.mode == "fm8")
    summary = format_summary(
        c10_findings,
        c11_findings,
        extra_clauses=[f"FM8 compliance-blindness (folded to retention/sinks): {fm8_count}."],
    )

    return AIQualityReport(
        c10_findings=c10_findings,
        c11_findings=c11_findings,
        summary=summary,
    )

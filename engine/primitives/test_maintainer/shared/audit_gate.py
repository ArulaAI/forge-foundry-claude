"""audit_gate — C10 + C11 audit sub-calls for proposed test edits.

Charter §3.4: every review-shaped primitive auto-invokes **both** C10 (FM1-FM5)
and C11 (FM6-FM10).  For S2 test-maintainer, every EditProposal must be audited
before acceptance.  FM5 findings (security) and FM9 findings (test-coverage
theater) cause the proposal to be dropped; all other findings surface as
warnings but do not drop the proposal.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from primitives.failure_mode_audit import (
    AuditConfig,
    audit_path,
)
from primitives.production_failure_audit import audit_production_path

if TYPE_CHECKING:
    from primitives.test_maintainer.shared.edit_proposal import EditProposal


def audit_proposal(proposal: EditProposal) -> tuple[bool, list[str]]:
    """Run C10 audit_path() against the proposed test content.

    Args:
        proposal: The EditProposal to audit (its after_content is what gets audited).

    Returns:
        (should_drop, warnings):
            should_drop: True if proposal must be dropped (FM5 violation).
            warnings: list of human-readable warnings (FM1-4 findings) that DON'T drop
                      the proposal but are surfaced in the report.
    """
    if not proposal.after_content.strip():
        # Empty content (e.g., delete verdict) — nothing to audit
        return False, []

    # Write after_content to a temp file with the same extension as test_file
    suffix = proposal.test_file.suffix or ".py"
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False, encoding="utf-8") as tf:
        tf.write(proposal.after_content)
        tmp_path = Path(tf.name)

    try:
        # C10: FM1-FM5 (common failure modes)
        c10_findings = audit_path(tmp_path, config=AuditConfig())
        # C11: FM6-FM10 (production failure modes) — §3.4 mandates both
        c11_findings = audit_production_path(tmp_path, config=AuditConfig())
    finally:
        tmp_path.unlink(missing_ok=True)

    # FM5 (security) and FM9 (test-coverage theater) are drop conditions.
    # FM9 is the most relevant production failure mode for test code — it
    # detects `assert True`, unconditionally-passing, mocking-the-SUT, etc.
    _DROP_MODES = {"fm5", "fm9"}
    drop_findings = [f for f in c10_findings if f.mode in _DROP_MODES]
    other_c10 = [f for f in c10_findings if f.mode not in _DROP_MODES]
    drop_c11 = [f for f in c11_findings if f.mode in _DROP_MODES]
    other_c11 = [f for f in c11_findings if f.mode not in _DROP_MODES]

    should_drop = len(drop_findings) > 0 or len(drop_c11) > 0
    warnings: list[str] = []
    for f in drop_findings:
        warnings.append(
            f"{f.mode.upper()} ({f.severity}) in proposed edit: {f.message} at line {f.line}"
        )
    for f in drop_c11:
        warnings.append(
            f"{f.mode.upper()} ({f.severity}) in proposed edit: {f.message} at line {f.line}"
        )
    for f in other_c10:
        warnings.append(
            f"{f.mode.upper()} ({f.severity}) in proposed edit: {f.message} at line {f.line}"
        )
    for f in other_c11:
        warnings.append(
            f"{f.mode.upper()} ({f.severity}) in proposed edit: {f.message} at line {f.line}"
        )
    return should_drop, warnings


def audit_proposals(
    proposals: list[EditProposal],
) -> tuple[list[EditProposal], list[tuple[EditProposal, list[str]]]]:
    """Run audit_proposal on each; partition into (accepted, dropped_with_warnings).

    Returns:
        accepted: proposals that passed (no FM5).
        dropped: list of (proposal, warnings) for proposals that were dropped.
    """
    accepted: list[EditProposal] = []
    dropped: list[tuple[EditProposal, list[str]]] = []
    for p in proposals:
        should_drop, warnings = audit_proposal(p)
        if should_drop:
            dropped.append((p, warnings))
        else:
            # Accepted; attach any FM1-FM4 warnings so they are surfaced in the report.
            accepted.append(replace(p, warnings=tuple(warnings)))
    return accepted, dropped

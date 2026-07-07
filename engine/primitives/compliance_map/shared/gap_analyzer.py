"""S70 /compliance-map — Stage E: gap analyzer.

Classifies each ControlMatch status as: addressed | partial | unaddressed | not_applicable
Cross-maps C11 FM8 (compliance-blindness) findings into PCI/GDPR gaps.

FM8 heuristic mapper:
  - Finding mentioning "PAN" (pan.in.logger, pan.in.schema) → PCI-DSS 3.5.1 gap
  - Finding mentioning "PII without retention" (pii.without.retention) → GDPR Art.5(1)(e) gap
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from primitives.compliance_map.shared.models import (
    Control,
    ControlMatch,
    ControlStatus,
    Framework,
    Gap,
)

if TYPE_CHECKING:
    from primitives.compliance_map.shared.ai_quality_aggregator import AIQualityReport
    from primitives.failure_mode_audit import Finding


# ---------------------------------------------------------------------------
# FM8 cross-mapping heuristics
# ---------------------------------------------------------------------------

# PCI-DSS 3.5.1 surrogate control for FM8 PAN-in-log gaps
_PCI_PAN_CONTROL = Control(
    id="3.5.1",
    framework="pci-dss-4.0",
    title="PAN is rendered unreadable anywhere it is stored",
    family="data-protection",
    severity="critical",
    evidence_hints=["tokenization or encryption of PAN at rest"],
    code_signatures=["KMS|AES.?256|tokenize|tokenization"],
    cwe_ids=["CWE-311", "CWE-312"],
)

# PCI-DSS 3.3.1 surrogate control for FM8 PAN-in-schema gaps
_PCI_SCHEMA_CONTROL = Control(
    id="3.3.1",
    framework="pci-dss-4.0",
    title="SAD is not retained after authorization",
    family="data-protection",
    severity="critical",
    evidence_hints=["no storage of PAN in plain DB schema"],
    code_signatures=["tokenize|tokenization"],
    cwe_ids=["CWE-312", "CWE-359"],
)

# GDPR Art.5(1)(e) surrogate control for FM8 PII-without-retention gaps
_GDPR_RETENTION_CONTROL = Control(
    id="Art.5.1.e",
    framework="gdpr",
    title="Storage limitation — data not kept longer than necessary",
    family="principles",
    severity="high",
    evidence_hints=["retention policy defined per data category with automated deletion"],
    code_signatures=["retention|ttl|TTL|purge|delete.after"],
    cwe_ids=["CWE-312"],
)


def _fm8_finding_to_gap(finding: Finding) -> Gap | None:
    """Attempt to cross-map a single C11 FM8 finding to a PCI-DSS or GDPR gap.

    Returns None if the finding doesn't map to a known compliance gap.
    """
    rule_id = finding.rule_id.lower()
    message = finding.message.lower()

    # PAN in logger → PCI-DSS 3.5.1
    if "pan" in rule_id and ("log" in rule_id or "logger" in rule_id):
        return Gap(
            control=_PCI_PAN_CONTROL,
            missing_evidence=(
                f"C11 FM8 detected PAN logged in plain text at "
                f"{finding.file}:{finding.line} — PCI-DSS prohibits logging PAN."
            ),
            suggested_remediation=(
                "Remove PAN from log statements. Use masked or tokenized form only. "
                "Apply structured log redaction (e.g., `masked_pan` field)."
            ),
            severity="critical",
        )

    # PAN in schema → PCI-DSS 3.3.1
    if "pan" in rule_id and "schema" in rule_id:
        return Gap(
            control=_PCI_SCHEMA_CONTROL,
            missing_evidence=(
                f"C11 FM8 detected PAN column in DB schema at "
                f"{finding.file}:{finding.line} — PCI-DSS prohibits plain PAN storage."
            ),
            suggested_remediation=(
                "Replace PAN column with a tokenized surrogate. "
                "Use a PCI-validated tokenization service or vault."
            ),
            severity="critical",
        )

    # PAN anywhere → PCI-DSS 3.5.1 (generic)
    if "pan" in message or "pan" in rule_id:
        return Gap(
            control=_PCI_PAN_CONTROL,
            missing_evidence=(
                f"C11 FM8 compliance-blindness finding: {finding.message} "
                f"at {finding.file}:{finding.line}"
            ),
            suggested_remediation=(
                "Ensure PAN is tokenized or encrypted before storage/transmission. "
                "PCI-DSS 3.5.1 requires PAN to be rendered unreadable everywhere it is stored."
            ),
            severity="critical",
        )

    # PII without retention → GDPR Art.5(1)(e)
    if "retention" in rule_id or "pii" in rule_id or "retention" in message:
        return Gap(
            control=_GDPR_RETENTION_CONTROL,
            missing_evidence=(
                f"C11 FM8 detected PII column without retention marker at "
                f"{finding.file}:{finding.line} — GDPR Art.5(1)(e) requires retention policy."
            ),
            suggested_remediation=(
                "Add a retention policy annotation to PII columns. "
                "Implement automated deletion or anonymisation when retention period expires. "
                "Document the retention period in your Records of Processing Activities (RoPA)."
            ),
            severity="high",
        )

    return None


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------


_MIN_EVIDENCE_FOR_ADDRESSED = 2


def _classify_status(match: ControlMatch) -> ControlStatus:
    """Classify control status based on evidence."""
    if not match.evidence:
        return "unaddressed"
    # If multiple evidence refs → addressed
    if len(match.evidence) >= _MIN_EVIDENCE_FOR_ADDRESSED:
        return "addressed"
    # Single evidence with high confidence → addressed
    single = match.evidence[0]
    if single.confidence in ("llm", "spec"):
        return "addressed"
    # Single regex hit without LLM confirmation → partial
    return "partial"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_gaps(
    matches: list[ControlMatch],
    ai_quality: AIQualityReport | None = None,
    selected_frameworks: list[Framework] | None = None,
) -> tuple[list[ControlMatch], list[Gap]]:
    """Stage E: classify status and surface gaps.

    Args:
        matches: ControlMatch objects from Stages C + D.
        ai_quality: Optional AIQualityReport from sub-call rule §3.4.
                    C11 FM8 findings are cross-mapped into PCI/GDPR gaps.
        selected_frameworks: Frameworks being analysed. Used to warn when
                             a framework has 0 touched controls (FM5 risk).

    Returns:
        Tuple of (updated matches with status set, gaps list).

    Note:
        - FM5 detection: if a framework has 0 touched controls, a warning is
          emitted (the spec may simply not address this framework at all, which
          is distinct from being fully compliant).
        - False-positive compliance claims (claiming 'addressed' without evidence)
          are the most dangerous failure mode. We err on the side of 'partial'.
    """
    # Classify status for each match
    updated_matches: list[ControlMatch] = []
    gaps: list[Gap] = []

    for match in matches:
        status = _classify_status(match)
        updated = ControlMatch(
            control=match.control,
            status=status,
            evidence=match.evidence,
            gap_description=match.gap_description,
            remediation_owner=match.remediation_owner,
        )
        updated_matches.append(updated)

        if status in ("unaddressed", "partial"):
            gaps.append(
                Gap(
                    control=match.control,
                    missing_evidence=(
                        match.gap_description
                        or f"Control {match.control.id} has no concrete code evidence."
                    ),
                    suggested_remediation=(
                        "Implement controls addressing: "
                        + "; ".join(match.control.evidence_hints[:2])
                        if match.control.evidence_hints
                        else f"Review requirements for {match.control.id}."
                    ),
                    severity=match.control.severity,
                )
            )

    # FM5 detection: warn if any framework has 0 touched controls
    if selected_frameworks:
        touched_frameworks = {m.control.framework for m in updated_matches}
        for fw in selected_frameworks:
            if fw not in touched_frameworks:
                print(
                    f"[compliance-map] WARNING (FM5): framework {fw!r} has 0 touched controls. "
                    f"This may indicate the spec does not address {fw} requirements at all, "
                    f"which is NOT the same as being fully compliant.",
                    file=sys.stderr,
                )

    # Cross-map C11 FM8 findings into PCI/GDPR gaps
    if ai_quality is not None:
        fm8_findings = [
            f for f in ai_quality.c11_findings if f.mode == "fm8" or f.rule_id.startswith("fm8")
        ]
        for finding in fm8_findings:
            gap = _fm8_finding_to_gap(finding)
            if gap is not None:
                print(
                    f"[compliance-map] FM8 cross-map: {finding.rule_id} → "
                    f"{gap.control.framework} {gap.control.id} gap",
                    file=sys.stderr,
                )
                gaps.append(gap)

    return updated_matches, gaps

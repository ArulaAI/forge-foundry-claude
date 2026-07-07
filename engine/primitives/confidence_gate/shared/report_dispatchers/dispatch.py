"""C6 /confidence-gate — top-level dispatcher.

Sniffs JSON structure to determine report type, then delegates
to the appropriate per-type dispatcher.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.confidence_gate.shared.report_dispatchers import (
    ai_quality,
    c4,
    c5,
    c8,
    s10,
    s11,
    s20,
    s30,
    s40,
    s60,
    s70,
)

if TYPE_CHECKING:
    from primitives.confidence_gate.shared.models import NormalizedFinding, ReportType

# ---------------------------------------------------------------------------
# Sniff → ReportType
# ---------------------------------------------------------------------------

# Each sniffer function takes a raw dict and returns True if it matches.
_SNIFFERS: list[tuple[str, object]] = [
    ("c4_code_review", c4.sniff),
    ("c5_spec_validate", c5.sniff),
    ("c8_architecture_review", c8.sniff),
    ("s10_threat_model", s10.sniff),
    ("s11_security_review", s11.sniff),
    ("s20_cloud_arch_review", s20.sniff),
    ("s30_frontend_design", s30.sniff),
    ("s40_api_contract_review", s40.sniff),
    ("s60_data_flow_map", s60.sniff),
    ("s70_compliance_map", s70.sniff),
    ("c10_failure_mode_audit", ai_quality.sniff_c10),
    ("c11_production_failure_audit", ai_quality.sniff_c11),
]

_DISPATCHERS: dict[str, object] = {
    "c4_code_review": c4.dispatch,
    "c5_spec_validate": c5.dispatch,
    "c8_architecture_review": c8.dispatch,
    "s10_threat_model": s10.dispatch,
    "s11_security_review": s11.dispatch,
    "s20_cloud_arch_review": s20.dispatch,
    "s30_frontend_design": s30.dispatch,
    "s40_api_contract_review": s40.dispatch,
    "s60_data_flow_map": s60.dispatch,
    "s70_compliance_map": s70.dispatch,
    "c10_failure_mode_audit": ai_quality.dispatch_c10,
    "c11_production_failure_audit": ai_quality.dispatch_c11,
}


def _looks_like_judgments_envelope(data: dict[str, object]) -> bool:
    """True if *data* looks like a skill-protocol judgments/prompts envelope (#885).

    These files share the envelope shape ``{schema_version, primitive, round,
    items:[...]}``. They are NOT primitive *report* JSON, so feeding a
    ``round-N-judgments.json`` (or ``round-N-prompts.json``) to confidence-gate
    is a common mistake — distinguished here so we can emit a clear, actionable
    rejection instead of the generic "cannot determine report type".
    """
    return (
        isinstance(data.get("items"), list)
        and "schema_version" in data
        and "primitive" in data
        and "round" in data
    )


#: Shared rejection message for the report-JSON-vs-judgment-envelope mix-up (#885).
_JUDGMENTS_ENVELOPE_REJECTION = (
    "this looks like a skill-protocol judgments/prompts envelope "
    "(schema_version/primitive/round/items), not a primitive report JSON. "
    "confidence-gate ingests an upstream review's REPORT JSON — re-run that "
    "review with `--format json` and feed its report, not the "
    "round-N-judgments.json / round-N-prompts.json file. "
    "See docs/skill-first-protocol.md."
)


def sniff_report_type(data: dict[str, object]) -> ReportType | None:
    """Sniff the report type from a parsed JSON dict.

    Returns None if no dispatcher matches.
    """
    for report_type, sniffer in _SNIFFERS:
        if callable(sniffer) and sniffer(data):
            return report_type  # type: ignore[return-value]
    return None


def dispatch_report(data: dict[str, object]) -> tuple[ReportType, list[NormalizedFinding]]:
    """Dispatch to the appropriate handler and return (report_type, findings).

    Raises:
        ValueError: If the report type cannot be determined.
    """
    report_type = sniff_report_type(data)
    if report_type is None:
        if _looks_like_judgments_envelope(data):
            raise ValueError(_JUDGMENTS_ENVELOPE_REJECTION)
        raise ValueError(f"Cannot determine report type from JSON keys: {list(data.keys())}")

    dispatcher = _DISPATCHERS[report_type]
    if callable(dispatcher):
        findings: list[NormalizedFinding] = dispatcher(data)
        return report_type, findings

    raise ValueError(f"No dispatcher for report type: {report_type}")

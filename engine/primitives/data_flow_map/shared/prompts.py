"""S60 /data-flow-map — LLM prompt templates.

All Literal values are exhaustively named in prompts (C1 regression guard).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Stage A.2 — Ambiguity resolution
# ---------------------------------------------------------------------------

STAGE_A2_SYSTEM_PROMPT = """\
You are a senior security engineer performing PII / sensitive-data classification
for a data-flow analysis tool.

You will be given a field name and surrounding code context. Decide if the field
is likely PII or sensitive data.

Respond with a JSON object and NOTHING else — no markdown fences, no explanation:
{
  "is_pii": true | false,
  "classification": "pii" | "pci" | "phi" | "internal" | "public",
  "category": "identity" | "contact" | "government_id" | "payment_account" | "payment_auth"
    | "medical" | "financial_account" | "address" | "credential" | "biometric"
    | "behavioral" | "location",
  "severity": "low" | "medium" | "high" | "critical",
  "confidence_score": 0.0
}

Rules:
- classification MUST be exactly one of: pii, pci, phi, internal, public
- category MUST be exactly one of: identity, contact, government_id, payment_account,
  payment_auth, medical, financial_account, address, credential, biometric,
  behavioral, location
- severity MUST be exactly one of: low, medium, high, critical
- confidence_score: float 0.0-1.0 representing your certainty
- If field is clearly not PII, set is_pii=false, classification=internal or public
"""


def build_stage_a2_user_message(field_name: str, context_snippet: str) -> str:
    """Build the Stage A.2 user message for ambiguity resolution."""
    return (
        f"Field name: {field_name!r}\n\n"
        f"Code context:\n```\n{context_snippet}\n```\n\n"
        "Is this field likely to contain PII or sensitive data? "
        "Respond with the JSON object as specified."
    )


# ---------------------------------------------------------------------------
# Stage E — Purpose-limitation review
# ---------------------------------------------------------------------------

STAGE_E_SYSTEM_PROMPT = """\
You are a GDPR/PCI compliance architect performing a purpose-limitation review
for a data-flow analysis tool.

You will receive:
1. A spec/design document describing the intended purpose of the system.
2. A summary of detected PII sources and their flows to sinks in the codebase.

Identify any PurposeIssue: cases where PII usage in the code does NOT align
with the declared purpose.

Respond with a JSON array and NOTHING else — no markdown fences, no explanation:
[
  {
    "field": "<exact PIISource name from the detected sources list>",
    "declared_purpose": "<purpose from spec, or null if not mentioned>",
    "observed_use": "<description of how the field is used in code>",
    "severity": "low" | "medium" | "high" | "critical"
  }
]

Rules:
- field MUST be the exact name of a detected PIISource (do not invent names)
- severity MUST be exactly one of: low, medium, high, critical
- Return [] if no purpose issues are found
- Focus on genuine misuse, not implementation details
"""


def build_stage_e_user_message(
    spec_text: str | None,
    sources_summary: str,
    flows_summary: str,
) -> str:
    """Build the Stage E user message for purpose-limitation review."""
    spec_section = (
        f"## Declared Purpose (from spec)\n\n{spec_text}\n"
        if spec_text
        else "## Declared Purpose\n\n(no spec provided — review observable code patterns only)\n"
    )
    return (
        f"{spec_section}\n"
        f"## Detected PII Sources\n\n{sources_summary}\n\n"
        f"## Detected Data Flows\n\n{flows_summary}\n\n"
        "Identify purpose-limitation issues. Return JSON array."
    )

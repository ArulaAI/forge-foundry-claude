"""SARIF 2.1.0 output renderer.

Maps Finding severity → SARIF level:
- low, medium → "warning"
- high, critical → "error"

CWE IDs (FM5 findings) attached at the run-level `taxa` array and referenced
per-result via `taxa[]` cross-reference.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from primitives.failure_mode_audit.shared.output.json_writer import JSON_SCHEMA_VERSION

if TYPE_CHECKING:
    from primitives.failure_mode_audit.shared.findings import Finding, Severity

_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA_URI = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/main/"
    "sarif-2.1/schema/sarif-schema-2.1.0.json"
)


def _severity_to_level(severity: Severity) -> str:
    return {"low": "warning", "medium": "warning", "high": "error", "critical": "error"}[severity]


def _build_taxonomies(findings: list[Finding]) -> list[dict[str, Any]]:
    """Build SARIF taxonomies array from CWE IDs in findings."""
    cwes = sorted({f.cwe_id for f in findings if f.cwe_id})
    if not cwes:
        return []
    return [
        {
            "name": "CWE",
            "version": "4.13",
            "informationUri": "https://cwe.mitre.org/",
            "taxa": [{"id": cwe, "name": cwe, "shortDescription": {"text": cwe}} for cwe in cwes],
        }
    ]


def _build_rules(findings: list[Finding]) -> list[dict[str, Any]]:
    """Build SARIF rules array — one per unique rule_id."""
    rule_ids = sorted({f.rule_id for f in findings})
    return [
        {
            "id": rid,
            "shortDescription": {"text": rid},
            "defaultConfiguration": {"level": "warning"},
        }
        for rid in rule_ids
    ]


def _build_result(finding: Finding) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ruleId": finding.rule_id,
        "level": _severity_to_level(finding.severity),
        "message": {"text": finding.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.file},
                    "region": {"startLine": finding.line},
                }
            }
        ],
    }
    if finding.cwe_id:
        result["taxa"] = [{"id": finding.cwe_id, "toolComponent": {"name": "CWE"}}]
    return result


def render_sarif(findings: list[Finding], *, tool_version: str) -> str:
    """Render findings as SARIF 2.1.0 JSON.

    Args:
        findings: Finding list.
        tool_version: forge-foundry-primitives package version
            (e.g., "0.2.0") — populated in SARIF tool.driver.version.
    """
    taxonomies = _build_taxonomies(findings)
    rules = _build_rules(findings)

    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "forge-foundry/failure-mode-audit",
                "version": tool_version,
                "informationUri": "https://github.com/arula-ai/forge-foundry-primitives",
                "rules": rules,
            }
        },
        "results": [_build_result(f) for f in findings],
    }
    if taxonomies:
        run["taxonomies"] = taxonomies

    sarif: dict[str, Any] = {
        "$schema": _SARIF_SCHEMA_URI,
        "version": _SARIF_VERSION,
        # Document-level property bag carrying OUR findings schema version (distinct
        # from the SARIF spec version above) so a SOX/PCI audit replay can pin which
        # C10/C11 schema shape produced this file (GOV-3, #619).
        "properties": {"schema_version": JSON_SCHEMA_VERSION},
        "runs": [run],
    }
    return json.dumps(sarif, indent=2, ensure_ascii=False)

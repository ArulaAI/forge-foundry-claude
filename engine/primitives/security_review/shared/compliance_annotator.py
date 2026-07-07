"""S11 /security-review — Compliance annotator (Stage D).

Validates each finding's compliance_refs against the loaded compliance.yaml.
Drops unknown refs with stderr warning (anti-hallucination S10/S70 lesson).

When no compliance.yaml is provided, findings pass through unchanged.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.security_review.shared.models import SecurityFinding

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_compliance_refs(compliance_yaml_path: Path | None) -> set[str] | None:
    """Load valid compliance ref strings from a compliance.yaml file.

    Args:
        compliance_yaml_path: Path to the compliance.yaml file, or None.

    Returns:
        Set of valid ref strings, or None if no path given / file missing.
    """
    if compliance_yaml_path is None:
        return None

    if not compliance_yaml_path.exists():
        return None

    try:
        raw_text = compliance_yaml_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[security-review] compliance_annotator: could not read {compliance_yaml_path}: {exc}",
            file=sys.stderr,
        )
        return None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        print(
            f"[security-review] compliance_annotator: YAML parse error in "
            f"{compliance_yaml_path}: {exc}",
            file=sys.stderr,
        )
        return None

    # Extract all control IDs from the compliance.yaml structure.
    # Support both flat list and nested dict formats.
    refs: set[str] = set()
    _collect_refs(data, refs)
    return refs if refs else None


def _collect_refs(data: Any, refs: set[str]) -> None:
    """Recursively collect string values that look like compliance refs."""
    if isinstance(data, str):
        refs.add(data)
    elif isinstance(data, list):
        for item in data:
            _collect_refs(item, refs)
    elif isinstance(data, dict):
        for key, value in data.items():
            # Keys like "pci-dss-4.0:3.5.1" are themselves refs
            if isinstance(key, str) and ":" in key:
                refs.add(key)
            _collect_refs(value, refs)


def annotate_compliance_refs(
    findings: list[SecurityFinding],
    compliance_yaml_path: Path | None = None,
    valid_refs: set[str] | None = None,
) -> list[SecurityFinding]:
    """Validate compliance_refs on findings against compliance.yaml.

    Anti-hallucination: drops unknown compliance_refs with stderr warning.

    Args:
        findings: List of SecurityFinding objects.
        compliance_yaml_path: Optional path to .foundry/compliance.yaml.
        valid_refs: Pre-loaded set of valid refs (overrides compliance_yaml_path).

    Returns:
        List of SecurityFinding with validated compliance_refs (may be rebuilt
        if refs were dropped). Findings without bad refs are returned as-is.
    """
    # Determine the valid refs set
    if valid_refs is None:
        valid_refs = load_compliance_refs(compliance_yaml_path)

    if valid_refs is None:
        # No compliance.yaml — pass through unchanged
        return list(findings)

    result: list[SecurityFinding] = []
    for finding in findings:
        if not finding.compliance_refs:
            result.append(finding)
            continue

        clean_refs: list[str] = []
        for ref in finding.compliance_refs:
            if ref in valid_refs:
                clean_refs.append(ref)
            else:
                print(
                    f"[security-review] anti-hallucination: dropping unknown "
                    f"compliance_ref {ref!r} on finding '{finding.title}' "
                    f"at {finding.file}:{finding.line}",
                    file=sys.stderr,
                )

        if clean_refs == list(finding.compliance_refs):
            # No change — reuse original
            result.append(finding)
        else:
            # Rebuild with cleaned refs
            result.append(finding.model_copy(update={"compliance_refs": clean_refs}))

    return result

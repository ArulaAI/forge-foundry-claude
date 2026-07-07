"""C6 /confidence-gate — policy loader.

Loads .foundry/gate-policy.yaml with default fallbacks.
No bare except-Exception, no tuple-form multi-exception catches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from primitives.confidence_gate.shared.models import GatePolicy

if TYPE_CHECKING:
    from pathlib import Path

_DEFAULT_POLICY = GatePolicy()

_DEFAULT_SEVERITY_WEIGHTS: dict[str, float] = {
    "low": 1.0,
    "medium": 4.0,
    "high": 10.0,
    "critical": 25.0,
}

_DEFAULT_CONFIDENCE_WEIGHTS: dict[str, float] = {
    "low": 0.3,
    "medium": 0.7,
    "high": 1.0,
}

_DEFAULT_THRESHOLDS: dict[str, float] = {
    "pass": 20.0,
    "review": 50.0,
}

# Default primitive weights per report type.
# ai_quality types (c10/c11) get 1.5x; all review-class get 1.0.
_DEFAULT_PRIMITIVE_WEIGHTS: dict[str, float] = {
    "c4_code_review": 1.0,
    "c5_spec_validate": 1.0,
    "c8_architecture_review": 1.0,
    "s10_threat_model": 1.0,
    "s11_security_review": 1.0,
    "s20_cloud_arch_review": 1.0,
    "s30_frontend_design": 1.0,
    "s40_api_contract_review": 1.0,
    "s60_data_flow_map": 1.0,
    "s70_compliance_map": 1.0,
    "c10_failure_mode_audit": 1.5,
    "c11_production_failure_audit": 1.5,
}


def get_primitive_weight(report_type: str, policy: GatePolicy) -> float:
    """Return the primitive weight for the given report_type from policy, with defaults."""
    if report_type in policy.primitive_weights:
        return policy.primitive_weights[report_type]
    return _DEFAULT_PRIMITIVE_WEIGHTS.get(report_type, 1.0)


def load_gate_policy(policy_path: Path | None = None) -> GatePolicy:
    """Load and return a GatePolicy from the given path (or defaults if not found).

    Args:
        policy_path: Path to a gate-policy.yaml file.
                     If None, returns the default policy.
                     If path does not exist, returns the default policy.

    Raises:
        ValueError: If the YAML file is malformed or fails schema validation.
    """
    if policy_path is None or not policy_path.exists():
        return _build_default_policy()

    raw_text = policy_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"gate-policy.yaml is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("gate-policy.yaml must be a YAML mapping at the top level")

    return _build_policy_from_dict(data)


def _build_default_policy() -> GatePolicy:
    """Return a fully-defaulted GatePolicy."""
    return GatePolicy(
        severity_weights=dict(_DEFAULT_SEVERITY_WEIGHTS),
        confidence_weights=dict(_DEFAULT_CONFIDENCE_WEIGHTS),
        primitive_weights={},
        thresholds=dict(_DEFAULT_THRESHOLDS),
    )


def _build_policy_from_dict(data: dict[str, object]) -> GatePolicy:
    """Build a GatePolicy from a raw YAML dict, merging with defaults."""
    severity_weights = dict(_DEFAULT_SEVERITY_WEIGHTS)
    raw_sw = data.get("severity_weights")
    if isinstance(raw_sw, dict):
        for k, v in raw_sw.items():
            if isinstance(k, str) and isinstance(v, (int, float)):
                severity_weights[k] = float(v)

    confidence_weights = dict(_DEFAULT_CONFIDENCE_WEIGHTS)
    raw_cw = data.get("confidence_weights")
    if isinstance(raw_cw, dict):
        for k, v in raw_cw.items():
            if isinstance(k, str) and isinstance(v, (int, float)):
                confidence_weights[k] = float(v)

    primitive_weights: dict[str, float] = {}
    raw_pw = data.get("primitive_weights")
    if isinstance(raw_pw, dict):
        for k, v in raw_pw.items():
            if isinstance(k, str) and isinstance(v, (int, float)):
                primitive_weights[k] = float(v)

    thresholds = dict(_DEFAULT_THRESHOLDS)
    raw_th = data.get("thresholds")
    if isinstance(raw_th, dict):
        for k, v in raw_th.items():
            if isinstance(k, str) and isinstance(v, (int, float)):
                thresholds[k] = float(v)

    try:
        return GatePolicy(
            severity_weights=severity_weights,
            confidence_weights=confidence_weights,
            primitive_weights=primitive_weights,
            thresholds=thresholds,
        )
    except ValueError as exc:
        raise ValueError(f"gate-policy.yaml failed schema validation: {exc}") from exc

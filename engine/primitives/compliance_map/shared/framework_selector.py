"""S70 /compliance-map — Framework selector.

Resolves the list of frameworks to analyse from:
1. --framework CLI flags (list of Framework literals)
2. .foundry/compliance.yaml (project-level config)
3. Falls back to all 4 frameworks if neither is provided.

Exit code 3 (framework-unresolvable) is raised by the CLI when neither source
provides a framework list. This module raises FrameworkResolutionError in that case.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import yaml

from primitives.compliance_map.shared.models import _ALL_FRAMEWORKS, Framework

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FrameworkResolutionError(Exception):
    """Raised when frameworks cannot be resolved from CLI flags or config file."""


# ---------------------------------------------------------------------------
# Config model (plain dataclass; Pydantic not needed for this small struct)
# ---------------------------------------------------------------------------


class ComplianceConfig:
    """Parsed .foundry/compliance.yaml content."""

    def __init__(
        self,
        frameworks: list[Framework],
        version_warnings: list[str],
    ) -> None:
        self.frameworks = frameworks
        self.version_warnings = version_warnings

    def __repr__(self) -> str:
        return f"ComplianceConfig(frameworks={self.frameworks!r})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_COMPLIANCE_CONFIG_PATHS = [
    ".foundry/compliance.yaml",
    ".foundry/compliance.yml",
]

_VALID_FRAMEWORKS: tuple[Framework, ...] = _ALL_FRAMEWORKS


def _load_compliance_yaml(config_path: Path) -> dict[str, Any]:
    """Load and parse .foundry/compliance.yaml."""
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise FrameworkResolutionError(f"Failed to parse {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise FrameworkResolutionError(
            f"Expected a YAML mapping in {config_path}, got {type(raw).__name__}"
        )
    return raw


def _parse_framework_list(raw: list[Any]) -> list[Framework]:
    """Validate and return a list of Framework literals."""
    result: list[Framework] = []
    for item in raw:
        s = str(item)
        if s not in _VALID_FRAMEWORKS:
            raise FrameworkResolutionError(
                f"Unknown framework {s!r} in config. Valid: {list(_VALID_FRAMEWORKS)}"
            )
        result.append(s)
    return result


def _detect_version_warnings(raw: dict[str, Any]) -> list[str]:
    """Detect FM6 (stale-pattern) version warnings from compliance.yaml.

    Warn if .foundry/compliance.yaml pins an older framework version than
    the canonical version shipped with this primitive.
    """
    _CANONICAL_VERSIONS: dict[str, str] = {
        "pci-dss-4.0": "4.0",
        "gdpr": "2016/679",
        "sox-404": "2002",
        "nist-800-53-rev5": "rev5",
        "epa-environmental": "cfr40",
    }
    warnings: list[str] = []
    pinned_versions: dict[str, Any] = raw.get("versions", {}) or {}
    for fw, canonical in _CANONICAL_VERSIONS.items():
        if fw in pinned_versions:
            pinned = str(pinned_versions[fw])
            if pinned != canonical:
                warnings.append(
                    f"[FM6-stale] .foundry/compliance.yaml pins {fw} "
                    f"to version {pinned!r} but canonical is {canonical!r}. "
                    f"Consider updating to the latest."
                )
    return warnings


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_frameworks(
    cli_frameworks: list[str] | None = None,
    config_path: Path | None = None,
    project_root: Path | None = None,
) -> list[Framework]:
    """Resolve the list of frameworks to analyse.

    Resolution order:
    1. CLI --framework flags (if provided, use these exclusively)
    2. .foundry/compliance.yaml frameworks list
    3. Default: all 4 frameworks

    Args:
        cli_frameworks: Frameworks passed via --framework flags (may be empty list).
        config_path: Explicit path to compliance config YAML.
        project_root: Project root for auto-detecting .foundry/compliance.yaml.

    Returns:
        Non-empty list of Framework literals.

    Raises:
        FrameworkResolutionError: If any framework string is invalid.
    """
    # 1. CLI flags take precedence
    if cli_frameworks:
        validated: list[Framework] = []
        for fw_str in cli_frameworks:
            if fw_str not in _VALID_FRAMEWORKS:
                raise FrameworkResolutionError(
                    f"Unknown --framework value {fw_str!r}. Valid: {list(_VALID_FRAMEWORKS)}"
                )
            validated.append(fw_str)
        return validated

    # 2. Try .foundry/compliance.yaml
    resolved_config: Path | None = config_path
    if resolved_config is None and project_root is not None:
        for rel in _COMPLIANCE_CONFIG_PATHS:
            candidate = project_root / rel
            if candidate.exists():
                resolved_config = candidate
                break

    if resolved_config is not None and resolved_config.exists():
        raw = _load_compliance_yaml(resolved_config)
        raw_frameworks = raw.get("frameworks", [])
        if raw_frameworks:
            parsed = _parse_framework_list(raw_frameworks)
            warnings = _detect_version_warnings(raw)
            for w in warnings:
                print(w, file=sys.stderr)
            return parsed

    # 3. Default: all frameworks
    return list(_ALL_FRAMEWORKS)


def load_compliance_config(config_path: Path) -> ComplianceConfig:
    """Load and validate a .foundry/compliance.yaml file.

    Returns:
        ComplianceConfig with frameworks and any version warnings.

    Raises:
        FrameworkResolutionError: If the config is malformed.
    """
    raw = _load_compliance_yaml(config_path)
    raw_frameworks = raw.get("frameworks", [])
    frameworks = _parse_framework_list(raw_frameworks) if raw_frameworks else list(_ALL_FRAMEWORKS)
    warnings = _detect_version_warnings(raw)
    return ComplianceConfig(frameworks=frameworks, version_warnings=warnings)

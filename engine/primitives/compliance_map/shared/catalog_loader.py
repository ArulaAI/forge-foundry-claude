"""S70 /compliance-map — Control catalog loader.

Loads YAML control catalogs from skill/references/catalogs/ and validates
each entry against the Control Pydantic model. Custom catalog extensions can be
merged via --config PATH (a YAML file extending the same schema).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from primitives.compliance_map.shared.models import _ALL_FRAMEWORKS, Control, Framework

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# The control catalogs are the shared data home for both paths: this in-session
# skill body cites them under references/catalogs/, and this CI Python reads them
# from skill/references/catalogs/. Resolve from this file:
#   primitives/compliance_map/shared/catalog_loader.py
#     → primitives/compliance_map/skill/references/catalogs/
_CATALOGS_DIR = Path(__file__).parent.parent / "skill" / "references" / "catalogs"
assert _CATALOGS_DIR.is_dir(), f"compliance-map catalogs directory missing: {_CATALOGS_DIR}"

_FRAMEWORK_FILES: dict[str, str] = {
    "pci-dss-4.0": "pci-dss-4.0.yaml",
    "gdpr": "gdpr.yaml",
    "sox-404": "sox-404-itgc.yaml",
    "nist-800-53-rev5": "nist-800-53-rev5.yaml",
    "epa-environmental": "epa-environmental.yaml",
}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ControlCatalog:
    """A loaded and validated set of controls for a single framework."""

    def __init__(
        self,
        framework: Framework,
        version: str,
        controls: list[Control],
    ) -> None:
        self.framework: Framework = framework
        self.version = version
        self.controls = controls

    def __repr__(self) -> str:
        return (
            f"ControlCatalog(framework={self.framework!r}, "
            f"version={self.version!r}, "
            f"controls={len(self.controls)})"
        )


class CatalogLoadError(Exception):
    """Raised when a catalog YAML is malformed or fails Pydantic validation."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_control(raw: dict[str, Any], framework: Framework) -> Control:
    """Parse and validate a single control dict from YAML."""
    # Inject the framework field (stored at catalog level, not per-control in YAML)
    raw_with_fw = dict(raw)
    raw_with_fw["framework"] = framework
    try:
        return Control(**raw_with_fw)
    except (TypeError, ValidationError) as exc:
        control_id = raw.get("id", "?")
        raise CatalogLoadError(f"Control {control_id!r} failed validation: {exc}") from exc


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load and parse a YAML file, raising CatalogLoadError on parse failure."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise CatalogLoadError(f"Failed to parse YAML at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CatalogLoadError(f"Expected a YAML mapping at top level in {path}, got {type(raw)}")
    return raw


def _validate_framework_literal(fw: str) -> Framework:
    """Validate that fw is a known Framework literal."""
    if fw not in _ALL_FRAMEWORKS:
        raise CatalogLoadError(
            f"Unknown framework {fw!r} in catalog. Valid values: {_ALL_FRAMEWORKS}"
        )
    return fw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_catalog(
    framework: Framework,
    extra_yaml: Path | None = None,
) -> ControlCatalog:
    """Load the shipped control catalog for *framework*, optionally merging *extra_yaml*.

    Args:
        framework: One of the four supported frameworks.
        extra_yaml: Optional path to a custom catalog YAML extending the shipped one.
                    Controls in extra_yaml are appended (no deduplication by id).

    Returns:
        ControlCatalog with all validated controls.

    Raises:
        CatalogLoadError: If the catalog file is missing, malformed, or has invalid controls.
    """
    filename = _FRAMEWORK_FILES.get(framework)
    if filename is None:
        raise CatalogLoadError(f"No catalog file configured for framework {framework!r}")

    catalog_path = _CATALOGS_DIR / filename
    if not catalog_path.exists():
        raise CatalogLoadError(f"Catalog file not found: {catalog_path}")

    raw = _load_yaml_file(catalog_path)

    # Validate top-level keys
    if "controls" not in raw:
        raise CatalogLoadError(f"Catalog {catalog_path} missing 'controls' list")

    # Parse version
    version = str(raw.get("version", "unknown"))

    # Parse controls
    controls: list[Control] = []
    raw_controls = raw.get("controls", [])
    if not isinstance(raw_controls, list):
        raise CatalogLoadError(f"Catalog {catalog_path}: 'controls' must be a list")

    for item in raw_controls:
        if not isinstance(item, dict):
            raise CatalogLoadError(
                f"Catalog {catalog_path}: control entry is not a mapping: {item!r}"
            )
        control = _parse_control(item, framework)
        controls.append(control)

    # Merge extra_yaml if provided
    if extra_yaml is not None:
        if not extra_yaml.exists():
            print(
                f"WARNING: extra catalog YAML not found: {extra_yaml} — skipping",
                file=sys.stderr,
            )
        else:
            extra_raw = _load_yaml_file(extra_yaml)
            extra_controls_raw = extra_raw.get("controls", [])
            if not isinstance(extra_controls_raw, list):
                raise CatalogLoadError(f"Extra catalog {extra_yaml}: 'controls' must be a list")
            # Override framework from extra_yaml's framework field if present
            extra_fw_str = extra_raw.get("framework", framework)
            extra_fw = _validate_framework_literal(str(extra_fw_str))
            for item in extra_controls_raw:
                if not isinstance(item, dict):
                    raise CatalogLoadError(
                        f"Extra catalog {extra_yaml}: entry is not a mapping: {item!r}"
                    )
                control = _parse_control(item, extra_fw)
                controls.append(control)

    print(
        f"[compliance-map] Loaded catalog {framework!r}: {len(controls)} control(s)",
        file=sys.stderr,
    )

    return ControlCatalog(framework=framework, version=version, controls=controls)

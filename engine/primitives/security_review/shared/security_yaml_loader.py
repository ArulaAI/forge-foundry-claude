"""S11 /security-review — Security YAML loader.

Loads .foundry/security.yaml which declares security-sensitive path hints.
Returns None when the path is None or the file is missing (heuristic-only mode).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class SecurityConfig(BaseModel):
    """Schema for .foundry/security.yaml."""

    model_config = ConfigDict(extra="forbid")

    sensitive_paths: list[str] = Field(default_factory=list)
    auth_modules: list[str] = Field(default_factory=list)
    crypto_modules: list[str] = Field(default_factory=list)
    secret_locations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_security_yaml(path: Path | None) -> SecurityConfig | None:
    """Load .foundry/security.yaml and return a SecurityConfig.

    Args:
        path: Path to the security.yaml file, or None.

    Returns:
        SecurityConfig if the file exists and is valid; None otherwise.
    """
    if path is None:
        return None

    if not path.exists():
        return None

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"[security-review] security_yaml_loader: could not read {path}: {exc}",
            file=sys.stderr,
        )
        return None

    try:
        data = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        print(
            f"[security-review] security_yaml_loader: YAML parse error in {path}: {exc}",
            file=sys.stderr,
        )
        return None

    if data is None:
        # Empty file — return empty config
        return SecurityConfig()

    if not isinstance(data, dict):
        print(
            f"[security-review] security_yaml_loader: expected mapping in {path}, "
            f"got {type(data).__name__}",
            file=sys.stderr,
        )
        return None

    try:
        return SecurityConfig(**data)
    except (TypeError, ValueError, ValidationError) as exc:
        print(
            f"[security-review] security_yaml_loader: schema error in {path}: {exc}",
            file=sys.stderr,
        )
        return None

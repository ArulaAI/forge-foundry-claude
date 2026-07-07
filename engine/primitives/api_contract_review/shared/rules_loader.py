"""S40 /api-contract-review — Rules loader.

Loads org-specific API contract overrides from .foundry/api-rules.yaml.
Falls back to empty config if the file does not exist.

Schema (all optional):
  disabled_lenses: [versioning, error_shapes, ...]   # skip these lenses
  severity_overrides:
    versioning: high                                  # override default severity
    rate_limiting: low
  custom_rfc_refs: [RFC 9110, RFC 6585]              # additional allowed RFC refs
  pagination:
    max_page_size: 200                               # org override
  rate_limiting:
    require_retry_after: true
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.api_contract_review.shared.models import APILens, Severity

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MAX_PAGE_SIZE: int = 100
_DEFAULT_REQUIRE_RETRY_AFTER: bool = False


class APIRulesConfig:
    """Parsed .foundry/api-rules.yaml configuration."""

    def __init__(
        self,
        disabled_lenses: list[APILens] | None = None,
        severity_overrides: dict[str, Severity] | None = None,
        custom_rfc_refs: list[str] | None = None,
        max_page_size: int = _DEFAULT_MAX_PAGE_SIZE,
        require_retry_after: bool = _DEFAULT_REQUIRE_RETRY_AFTER,
    ) -> None:
        self.disabled_lenses: list[APILens] = disabled_lenses or []
        self.severity_overrides: dict[str, Severity] = severity_overrides or {}
        self.custom_rfc_refs: list[str] = custom_rfc_refs or []
        self.max_page_size: int = max_page_size
        self.require_retry_after: bool = require_retry_after

    def is_lens_disabled(self, lens: APILens) -> bool:
        """Return True if *lens* is in the disabled_lenses list."""
        return lens in self.disabled_lenses

    def severity_for_lens(self, lens: APILens, default: Severity) -> Severity:
        """Return the severity override for *lens*, or *default* if not set."""
        override = self.severity_overrides.get(lens)
        if override is not None:
            return override
        return default


_VALID_LENSES: frozenset[str] = frozenset(
    {
        "versioning",
        "error_shapes",
        "auth_spec",
        "idempotency",
        "backward_compatibility",
        "pagination",
        "rate_limiting",
    }
)
_VALID_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})


def _parse_raw(raw: dict[str, Any]) -> APIRulesConfig:
    """Parse a raw YAML dict into APIRulesConfig."""
    disabled_raw = raw.get("disabled_lenses") or []
    from typing import cast  # noqa: PLC0415

    disabled_lenses: list[APILens] = cast(
        "list[APILens]",
        [lens for lens in disabled_raw if lens in _VALID_LENSES],
    )

    sev_raw = raw.get("severity_overrides") or {}
    severity_overrides: dict[str, Severity] = cast(
        "dict[str, Severity]",
        {k: v for k, v in sev_raw.items() if k in _VALID_LENSES and v in _VALID_SEVERITIES},
    )

    custom_rfc_refs = [str(r) for r in (raw.get("custom_rfc_refs") or [])]

    pagination = raw.get("pagination") or {}
    max_page_size_raw = pagination.get("max_page_size", _DEFAULT_MAX_PAGE_SIZE)
    max_page_size = (
        int(max_page_size_raw)
        if isinstance(max_page_size_raw, (int, float))
        else _DEFAULT_MAX_PAGE_SIZE
    )

    rate = raw.get("rate_limiting") or {}
    require_retry_after = bool(rate.get("require_retry_after", _DEFAULT_REQUIRE_RETRY_AFTER))

    return APIRulesConfig(
        disabled_lenses=disabled_lenses,
        severity_overrides=severity_overrides,
        custom_rfc_refs=custom_rfc_refs,
        max_page_size=max_page_size,
        require_retry_after=require_retry_after,
    )


def load_api_rules(
    contract_path: Path,
    rules_yaml_path: Path | None = None,
) -> APIRulesConfig:
    """Load .foundry/api-rules.yaml from the contract's directory tree.

    Search order:
      1. Explicit *rules_yaml_path* if given.
      2. .foundry/api-rules.yaml relative to *contract_path* (if dir) or its parent.
      3. Return empty (default) config if not found.

    Args:
        contract_path: Path to the contract file or directory.
        rules_yaml_path: Optional explicit path to api-rules.yaml.

    Returns:
        APIRulesConfig with parsed overrides (or defaults if file not found).
    """
    if rules_yaml_path is not None and rules_yaml_path.exists():
        raw = yaml.safe_load(rules_yaml_path.read_text(encoding="utf-8")) or {}
        return _parse_raw(raw)

    # Auto-discover
    base = contract_path if contract_path.is_dir() else contract_path.parent
    candidate = base / ".foundry" / "api-rules.yaml"
    if candidate.exists():
        raw = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        return _parse_raw(raw)

    return APIRulesConfig()

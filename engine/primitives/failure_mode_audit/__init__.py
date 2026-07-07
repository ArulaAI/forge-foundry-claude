"""C10 — /failure-mode-audit primitive public API.

Vendored from forge-prowess-primitives@v0.4.0 (U1) — see CHANGELOG-VENDOR.md.
"""

from primitives.failure_mode_audit.shared.auditor import audit_path, filter_by_severity
from primitives.failure_mode_audit.shared.config import (
    AuditConfig,
    ConfigParseError,
    Fm6Config,
    Fm7Config,
    Fm8WarnConfig,
    Fm9Config,
    Fm10Config,
    ProductionConfig,
)
from primitives.failure_mode_audit.shared.findings import SEVERITY_ORDER, Finding, Severity
from primitives.failure_mode_audit.shared.llm_judge import (
    BudgetExceededError,
    LlmCallCounter,
)

__all__ = [
    "SEVERITY_ORDER",
    "AuditConfig",
    "BudgetExceededError",
    "ConfigParseError",
    "Finding",
    "Fm6Config",
    "Fm7Config",
    "Fm8WarnConfig",
    "Fm9Config",
    "Fm10Config",
    "LlmCallCounter",
    "ProductionConfig",
    "Severity",
    "audit_path",
    "filter_by_severity",
]

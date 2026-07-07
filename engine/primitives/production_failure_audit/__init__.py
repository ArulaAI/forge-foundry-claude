"""C11 — /production-failure-audit primitive public API."""

from primitives.failure_mode_audit import AuditConfig, ConfigParseError
from primitives.production_failure_audit.shared.auditor import audit_production_path
from primitives.production_failure_audit.shared.findings import (
    SEVERITY_ORDER,
    Finding,
    Severity,
)
from primitives.production_failure_audit.shared.llm_judge import (
    BudgetExceededError,
    LlmCallCounter,
)

__all__ = [
    "SEVERITY_ORDER",
    "AuditConfig",
    "BudgetExceededError",
    "ConfigParseError",
    "Finding",
    "LlmCallCounter",
    "Severity",
    "audit_production_path",
]

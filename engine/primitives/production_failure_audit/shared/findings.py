"""Re-export Finding/Severity/SEVERITY_ORDER from C10 for C11 consumers."""

from primitives.failure_mode_audit import (
    SEVERITY_ORDER,
    Finding,
    Severity,
)

__all__ = ["SEVERITY_ORDER", "Finding", "Severity"]

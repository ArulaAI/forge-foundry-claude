"""C10 audit_path() — the dispatcher that combines all FM1-FM5 detectors.

Vendored from forge-prowess-primitives@v0.4.0 (auditor.py:audit_path) — see
CHANGELOG-VENDOR.md for divergence. Foundry refactor: per-stack dispatch into
stacks/, config-driven via AuditConfig, optional LLM hook for FM3/FM4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.failure_mode_audit.shared.config import AuditConfig
from primitives.failure_mode_audit.shared.findings import SEVERITY_ORDER, Finding, Severity
from primitives.failure_mode_audit.shared.fm2 import audit_python_fm2
from primitives.failure_mode_audit.shared.regex_rules import audit_regex_mode
from primitives.failure_mode_audit.shared.stacks.java import audit_java_fm1
from primitives.failure_mode_audit.shared.stacks.nodejs import audit_nodejs_fm1
from primitives.failure_mode_audit.shared.stacks.python import audit_python_fm1

if TYPE_CHECKING:
    from pathlib import Path


def audit_path(
    path: Path,
    *,
    config: AuditConfig | None = None,
    diff_against: str | None = None,  # noqa: ARG001 — reserved for v0.2.0 diff-aware mode
) -> list[Finding]:
    """Audit *path* for AI failure modes per *config*; return findings list.

    Deterministic FM1-FM5 detectors only. LLM-judged FM3/FM4 run via the
    skill-first extract→judge→continue protocol (see the CLI command), not here.

    Args:
        path: Directory or file to audit.
        config: Loaded from .foundry/failure-modes.yaml; None = defaults.
        diff_against: Git ref for diff-aware mode (NotImplementedError in v0.2.0).
    """
    if config is None:
        config = AuditConfig()

    project_root = path if path.is_dir() else path.parent
    all_findings: list[Finding] = []

    if config.fm1_hallucinated_deps.enabled:
        all_findings.extend(audit_python_fm1(path, project_root=project_root, config=config))
        all_findings.extend(audit_nodejs_fm1(path, project_root=project_root, config=config))
        all_findings.extend(audit_java_fm1(path, project_root=project_root, config=config))

    if config.fm2_context_window.enabled:
        all_findings.extend(audit_python_fm2(path, project_root=project_root, config=config))

    for mode in ("fm3", "fm4", "fm5"):
        all_findings.extend(audit_regex_mode(path, mode=mode, config=config))

    # NOTE: full per-candidate LLM dispatch from audit_path is v0.3.0 work.
    # judge_fm3_candidate / judge_fm4_function are in llm_judge.py and tested
    # standalone — they can be invoked directly by sub-callers that need them.
    # See design spec §13 + plan acceptance criteria.

    return all_findings


def filter_by_severity(findings: list[Finding], min_severity: Severity) -> list[Finding]:
    """Return findings whose severity >= *min_severity*."""
    threshold = SEVERITY_ORDER[min_severity]
    return [f for f in findings if SEVERITY_ORDER[f.severity] >= threshold]

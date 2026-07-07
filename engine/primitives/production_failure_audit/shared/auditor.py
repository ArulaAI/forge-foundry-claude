"""C11 — audit_production_path() dispatcher.

Composes FM6-FM10 deterministic detectors. LLM-judged FM6/FM7/FM10 run via the
skill-first extract→judge→continue protocol (see the CLI command), not here.

Refs: GH issue #77
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.failure_mode_audit import AuditConfig
from primitives.production_failure_audit.shared.fm6_stale_pattern import audit_fm6
from primitives.production_failure_audit.shared.fm7_overgeneralization import audit_fm7
from primitives.production_failure_audit.shared.fm8_compliance_blindness import audit_fm8
from primitives.production_failure_audit.shared.fm9_test_theater import audit_fm9
from primitives.production_failure_audit.shared.fm10_arch_decay import audit_fm10

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.production_failure_audit.shared.findings import Finding


def audit_production_path(
    path: Path,
    *,
    config: AuditConfig | None = None,
) -> list[Finding]:
    """Audit *path* for production failure modes (FM6-FM10), deterministically.

    Mirrors audit_path() from C10 but covers C11's FMs. config.production
    sub-fields control each mode.
    """
    if config is None:
        config = AuditConfig()

    project_root = path if path.is_dir() else path.parent
    all_findings: list[Finding] = []

    all_findings.extend(audit_fm6(path, config=config))
    all_findings.extend(audit_fm7(path, config=config))
    all_findings.extend(audit_fm8(path, config=config))
    all_findings.extend(audit_fm9(path, config=config))
    all_findings.extend(audit_fm10(path, project_root=project_root, config=config))

    return all_findings

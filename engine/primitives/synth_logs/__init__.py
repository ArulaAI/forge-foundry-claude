"""M6 /mod-synth-logs — public API.

Pydantic models for the four-phase pipeline (Phase A ingest, Phase B
plan, Phase C generate, Phase D validate) are exposed here (M6-MODELS-1
/ #463). The phase modules (``shared/ingest.py``, ``shared/plan.py``,
``shared/generate.py``, ``shared/validate.py``) and the orchestrator CLI
(``shared/cli.py``) land with the M6-PHASE-* / M6-CLI-* sub-issues.

See ``primitives/synth_logs/SPEC.md`` for the primitive contract and
``docs/superpowers/specs/2026-06-02-mainframe-playbook-synth-logs-design.md``
§M6 for the canonical design.
"""

from primitives.synth_logs.shared.models import (
    EntitiesDigest,
    GeneratedFileRecord,
    IncidentEvent,
    IncidentEventKind,
    LogFileSpec,
    LogFormat,
    ManifestRecord,
    Scenario,
    ScenarioPlan,
    ValidationReport,
    ValidationRuleKind,
    ValidationRuleResult,
)

__all__ = [
    "EntitiesDigest",
    "GeneratedFileRecord",
    "IncidentEvent",
    "IncidentEventKind",
    "LogFileSpec",
    "LogFormat",
    "ManifestRecord",
    "Scenario",
    "ScenarioPlan",
    "ValidationReport",
    "ValidationRuleKind",
    "ValidationRuleResult",
]

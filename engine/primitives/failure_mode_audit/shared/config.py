"""AuditConfig — Pydantic v2 model for .foundry/failure-modes.yaml.

Foundry-native module. Read by AuditConfig.from_project() (Task 5).
Used by audit_path() to drive per-FM enable flags + LLM mode toggles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

if TYPE_CHECKING:
    from pathlib import Path

FmDetectMode: TypeAlias = Literal["regex", "llm", "both"]


class FmConfig(BaseModel):
    """Per-FM toggle: enabled flag + detection mode (regex/llm/both)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: FmDetectMode = "regex"


class Fm2Config(BaseModel):
    """FM2 has thresholds in addition to the enabled flag (no mode toggle)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    threshold_target_lines: int = 1500
    threshold_dir_hops: int = 3


class Fm5Config(BaseModel):
    """FM5 is always static (no mode toggle), only an enabled flag."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class LlmConfig(BaseModel):
    """LLM cost gating."""

    model_config = ConfigDict(extra="forbid")

    max_calls_per_audit: int = 10


class ProjectConfig(BaseModel):
    """Project-specific extensions (e.g., FM1 OK toplevel whitelist additions)."""

    model_config = ConfigDict(extra="forbid")

    ok_toplevels: list[str] = []


class Fm6Config(BaseModel):
    """FM6 stale-pattern detection."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: FmDetectMode = "regex"


class Fm7Config(BaseModel):
    """FM7 overgeneralization detection."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: FmDetectMode = "regex"


class Fm8WarnConfig(BaseModel):
    """FM8 compliance blindness detection — warn-only until S70 ships."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    warn_only: bool = True


class Fm9Config(BaseModel):
    """FM9 test-coverage theater detection (always static)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True


class Fm10Config(BaseModel):
    """FM10 architectural decay detection."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: FmDetectMode = "regex"
    architecture_yaml: str = ".foundry/architecture.yaml"


class ProductionConfig(BaseModel):
    """C11 — FM6-FM10 (dangerous production failure modes)."""

    model_config = ConfigDict(extra="forbid")

    fm6_stale_pattern: Fm6Config = Fm6Config()
    fm7_overgeneralization: Fm7Config = Fm7Config()
    fm8_compliance_blindness: Fm8WarnConfig = Fm8WarnConfig()
    fm9_test_theater: Fm9Config = Fm9Config()
    fm10_arch_decay: Fm10Config = Fm10Config()


class ConfigParseError(ValueError):
    """Raised when .foundry/failure-modes.yaml is malformed or fails schema validation."""


class AuditConfig(BaseModel):
    """Top-level config for the failure-mode auditor.

    Loaded from `<project_root>/.foundry/failure-modes.yaml` via from_project().
    All fields optional with safe defaults.
    """

    model_config = ConfigDict(extra="forbid")

    fm1_hallucinated_deps: FmConfig = FmConfig()
    fm2_context_window: Fm2Config = Fm2Config()
    fm3_training_bias: FmConfig = FmConfig()
    fm4_overconfident: FmConfig = FmConfig()
    fm5_security_propagation: Fm5Config = Fm5Config()
    llm: LlmConfig = LlmConfig()
    project: ProjectConfig = ProjectConfig()
    production: ProductionConfig = ProductionConfig()

    # Scan-scoping flag (#1192). Default False: dependency dirs (.venv,
    # node_modules, …) AND the wholly-vendored harness trees (.foundry, .claude)
    # + emitted skill/rule subpaths are skipped during file walks so an audit of
    # a repo with a local virtualenv surfaces real findings, not thousands of
    # third-party ones. Set True (via the --include-vendored CLI flag) to
    # re-include the vendored harness trees; dependency dirs are never
    # re-included. Threaded to primitives._shared.scan_exclusions.
    include_vendored: bool = False

    def requires_llm(self) -> bool:
        """Return True if any FM has mode=llm or mode=both."""
        core = self.fm3_training_bias.mode in ("llm", "both") or self.fm4_overconfident.mode in (
            "llm",
            "both",
        )
        prod = (
            self.production.fm6_stale_pattern.mode in ("llm", "both")
            or self.production.fm7_overgeneralization.mode in ("llm", "both")
            or self.production.fm10_arch_decay.mode in ("llm", "both")
        )
        return core or prod

    @classmethod
    def from_project(cls, project_root: Path) -> AuditConfig:
        """Read .foundry/failure-modes.yaml from *project_root*.

        Returns defaults when file is absent. Raises ConfigParseError on
        malformed YAML, unknown keys, or invalid field values.
        """
        config_path = project_root / ".foundry" / "failure-modes.yaml"
        if not config_path.is_file():
            return cls()
        return _load_audit_config_from_file(config_path)


def _load_audit_config_from_file(path: Path) -> AuditConfig:
    """Load AuditConfig from a YAML file at *path*."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigParseError(f"Failed to parse {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigParseError(f"{path} must be a YAML mapping, got {type(raw).__name__}")
    try:
        return AuditConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigParseError(f"{path} fails schema: {exc}") from exc

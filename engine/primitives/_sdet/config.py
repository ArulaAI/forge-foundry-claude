"""SdetConfig — Pydantic v2 model for .foundry/sdet.yaml.

Foundry-native module. Read by SdetConfig.from_project() to drive S1
sdet-architect + S2 test-maintainer per-primitive enabled flags + mode
toggles + budgets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Self, TypeAlias

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from primitives._sdet.test_layer import TARGET_CONE_SUM, ConeRatios

if TYPE_CHECKING:
    from pathlib import Path

S1Mode: TypeAlias = Literal["llm", "template"]
S2Mode: TypeAlias = Literal["regex", "llm"]
TestDataLib: TypeAlias = Literal["faker", "hypothesis", "both"]
TestcontainersPolicy: TypeAlias = Literal["auto", "always", "never"]
CiPostMode: TypeAlias = Literal["pr-comment", "follow-up-pr"]


class ConeRatiosConfig(BaseModel):
    """Per-FM cone-ratio override; validates that all four integers sum to 100."""

    model_config = ConfigDict(extra="forbid")

    unit: int = 70
    contract: int = 15
    integration: int = 12
    e2e: int = 3

    @model_validator(mode="after")
    def _check_sum(self) -> ConeRatiosConfig:
        total = self.unit + self.contract + self.integration + self.e2e
        if total != TARGET_CONE_SUM:
            raise ValueError(
                f"cone_ratios must sum to {TARGET_CONE_SUM}, got {total} "
                f"(unit={self.unit} + contract={self.contract} + "
                f"integration={self.integration} + e2e={self.e2e})"
            )
        return self

    def to_cone_ratios(self) -> ConeRatios:
        return ConeRatios(
            unit=self.unit,
            contract=self.contract,
            integration=self.integration,
            e2e=self.e2e,
        )


class S1Config(BaseModel):
    """S1 sdet-architect config."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: S1Mode = "llm"
    coverage_target: int = 80
    cone_ratios: ConeRatiosConfig = ConeRatiosConfig()
    test_data: TestDataLib = "faker"
    testcontainers: TestcontainersPolicy = "auto"


class S2E2EConfig(BaseModel):
    """S2 e2e-maintenance config (#1029). Opt-in; e2e is slow/flaky."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    map_path: str = ".foundry/e2e-map.yaml"
    max_specs_per_run: int = 20
    timeout_seconds: int = 600


class S2Config(BaseModel):
    """S2 test-maintainer config."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    mode: S2Mode = "regex"
    max_files_per_run: int = 20
    apply_default: bool = False
    ci_post_mode: CiPostMode = "pr-comment"
    #: Bounded refine attempts in the green-gate verify loop (S2 TM-5, #897).
    #: A red edit is re-judged in-session up to this many times before it is
    #: reverted + sent to "needs human."
    max_refine: int = 2
    #: Safety cap on per-test-file coverage isolation runs in one refresh of
    #: the cached coverage index (S2 CP-1, #918). If a cold build / staleness
    #: sweep would exceed this, the coverage layer falls back to the static
    #: graph (``measured=False``) rather than run hundreds of isolation
    #: invocations / hang CI.
    max_coverage_runs: int = 50
    #: Opt-in e2e maintenance (#1029): the declared route->spec map + the
    #: Playwright verify run-adapter. Off by default (e2e is slow/flaky).
    e2e: S2E2EConfig = S2E2EConfig()


class LlmBudgetConfig(BaseModel):
    """Combined LLM budget across S1 + S2 per invocation."""

    model_config = ConfigDict(extra="forbid")

    max_calls_per_run: int = 50


class SdetConfig(BaseModel):
    """Top-level config for S1 sdet-architect + S2 test-maintainer."""

    model_config = ConfigDict(extra="forbid")

    s1: S1Config = S1Config()
    s2: S2Config = S2Config()
    llm: LlmBudgetConfig = LlmBudgetConfig()

    def requires_llm(self) -> bool:
        """True iff S1 or S2 has an LLM-using mode enabled."""
        s1_llm = self.s1.enabled and self.s1.mode == "llm"
        s2_llm = self.s2.enabled and self.s2.mode == "llm"
        return s1_llm or s2_llm

    @classmethod
    def from_project(cls, project_root: Path) -> Self:
        """Read .foundry/sdet.yaml from project_root; defaults if absent."""
        config_path = project_root / ".foundry" / "sdet.yaml"
        if not config_path.is_file():
            return cls()
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ConfigParseError(f"Failed to parse {config_path}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigParseError(
                f"{config_path} must be a YAML mapping, got {type(raw).__name__}"
            )
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ConfigParseError(f"{config_path} fails schema: {exc}") from exc


class ConfigParseError(ValueError):
    """Raised when .foundry/sdet.yaml is malformed or fails schema validation."""

"""DigestConfig + PlatformConfig — Pydantic v2 models for repo_digest YAML files.

DigestConfig  → .foundry/repo-digest.yaml  (returns defaults when absent)
PlatformConfig → .foundry/platform.yaml    (returns None when absent — federation is opt-in)

Both models use extra="forbid"; any unknown key raises ConfigParseError.
YAML parse errors and Pydantic ValidationErrors are re-raised as ConfigParseError.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal, Self, TypeAlias

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from primitives.repo_digest.shared.url_policy import (
    PeerUrlPolicyError,
    validate_peer_url,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

DigestMode: TypeAlias = Literal["template", "llm"]
FetchBackend: TypeAlias = Literal["git", "gh-api", "s3", "azure-blob", "gcs"]


# ---------------------------------------------------------------------------
# DigestConfig sub-models
# ---------------------------------------------------------------------------


class PerFileBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_digest: int = 8000
    conventions: int = 4000
    api_surface: int = 8000
    entry_points: int = 4000
    recent_deltas: int = 4000
    dependency_map: int = 2000


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_tokens: int = 30000
    per_file: PerFileBudget = PerFileBudget()


class RankingWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: float = 0.2
    recent: float = 0.3
    entry: float = 0.3
    api: float = 0.2


class RecentDeltasOpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = 30


class ApiSurfaceOpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_private: bool = False


class ConventionsOpts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    languages: list[str] = Field(default_factory=lambda: ["auto"])


class ExtractorsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recent_deltas: RecentDeltasOpts = RecentDeltasOpts()
    api_surface: ApiSurfaceOpts = ApiSurfaceOpts()
    conventions: ConventionsOpts = ConventionsOpts()


class LlmBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_calls_per_run: int = 5


class FederationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_ttl_minutes: int = 60
    shallow: bool = True


# ---------------------------------------------------------------------------
# Shared error
# ---------------------------------------------------------------------------


class ConfigParseError(ValueError):
    """Raised when config YAML is malformed or fails schema validation."""


# ---------------------------------------------------------------------------
# DigestConfig
# ---------------------------------------------------------------------------


class DigestConfig(BaseModel):
    """Top-level config for the repo_digest primitive.

    Loaded from `<project_root>/.foundry/repo-digest.yaml` via from_project().
    All fields optional with safe defaults — file absence is not an error.
    """

    model_config = ConfigDict(extra="forbid")

    mode: DigestMode = "template"
    budget: BudgetConfig = BudgetConfig()
    ranking_weights: RankingWeights = RankingWeights()
    extractors: ExtractorsConfig = ExtractorsConfig()
    llm: LlmBudget = LlmBudget()
    federation: FederationConfig = FederationConfig()

    @classmethod
    def from_project(cls, project_root: Path) -> Self:
        """Read .foundry/repo-digest.yaml from *project_root*.

        Returns defaults when file is absent. Raises ConfigParseError on
        malformed YAML, unknown keys, or invalid field values.
        """
        config_path = project_root / ".foundry" / "repo-digest.yaml"
        if not config_path.is_file():
            return cls()
        return _load_yaml_into(cls, config_path)


# ---------------------------------------------------------------------------
# PlatformConfig sub-models
# ---------------------------------------------------------------------------


class FetchOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str = "_foundry-context"
    shallow: bool = True

    @field_validator("branch")
    @classmethod
    def _valid_refname(cls, v: str) -> str:
        # SEC-8 (#172): branch flows to `git clone --branch <v>`. git rejects a
        # leading `-`, but validate at the config boundary too (symmetry with the
        # strict member_repos URL policy), and forbid refname metacharacters.
        if v.startswith("-") or not re.match(r"^[A-Za-z0-9_./-]+$", v):
            raise ValueError(f"invalid branch refname: {v!r}")
        return v


# ---------------------------------------------------------------------------
# PlatformConfig
# ---------------------------------------------------------------------------


class PlatformConfig(BaseModel):
    """Platform federation config for the repo_digest primitive.

    Loaded from `<project_root>/.foundry/platform.yaml` via from_project().
    Returns None when file is absent — federation requires explicit declaration.

    Each entry in ``member_repos`` is validated against the federation URL policy
    (see :mod:`primitives.repo_digest.shared.url_policy`). Plain ``org/name`` slugs
    are always permitted (cloned from github.com); fully-qualified HTTPS and SSH
    URLs are permitted only when the host appears in ``allowed_hosts``.
    """

    model_config = ConfigDict(extra="forbid")

    platform_id: str
    member_repos: list[str]
    allowed_hosts: list[str] = []
    fetch_backend: FetchBackend = "git"
    fetch_options: FetchOptions = FetchOptions()

    @model_validator(mode="after")
    def _validate_member_repos(self) -> PlatformConfig:
        allowed = frozenset(self.allowed_hosts)
        for repo in self.member_repos:
            try:
                validate_peer_url(repo, allowed_hosts=allowed)
            except PeerUrlPolicyError as exc:
                raise ValueError(f"member_repos entry rejected: {exc}") from exc
        return self

    @classmethod
    def from_project(cls, project_root: Path) -> PlatformConfig | None:
        """Return None when .foundry/platform.yaml is absent."""
        config_path = project_root / ".foundry" / "platform.yaml"
        if not config_path.is_file():
            return None
        return _load_yaml_into(cls, config_path)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_yaml_into[T: BaseModel](model: type[T], path: Path) -> T:
    """Parse YAML at *path* and validate it into *model*."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigParseError(f"Failed to parse {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigParseError(f"{path} must be a YAML mapping, got {type(raw).__name__}")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise ConfigParseError(f"{path} fails schema: {exc}") from exc

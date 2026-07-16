"""Manifest reader for .foundry/manifest.yaml.

Vendored from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from forge_sync.emitter.models import HARNESSES as _HARNESSES

if TYPE_CHECKING:
    from pathlib import Path


DEFAULT_HARNESSES: tuple[str, ...] = _HARNESSES
"""Default harness targets when ``harnesses:`` is omitted from the manifest.

Derived from :data:`forge_sync.emitter.models.HARNESSES` — the canonical
single source of truth for the five built-in harness names.
"""


class ManifestNotFound(FileNotFoundError):
    """Raised when .foundry/manifest.yaml does not exist in the project root."""


class ManifestParseError(ValueError):
    """Raised when .foundry/manifest.yaml is invalid YAML or fails schema validation."""


class PrimitiveOverride(BaseModel):
    """Tagged override entry for ``pinned_overrides`` (FLEX-5, #177).

    Lets a consumer pin a single primitive at a specific *source* (git URL or
    filesystem path) and *version* (git tag/branch when *source* is a URL),
    enabling multi-library composition without manually pre-resolving each
    primitive to a disk path. The legacy ``str`` shape (a bare path) is still
    accepted by ``SyncManifest.pinned_overrides`` for back-compat.
    """

    model_config = ConfigDict(extra="forbid")

    source: str  # git URL OR filesystem path
    version: str | None = None  # tag/branch when source is a URL

    @field_validator("version")
    @classmethod
    def _reject_unsafe_version(cls, v: str | None) -> str | None:
        """SEC-7: reject a *version* that could escape the foundry cache.

        ``version`` flows into ``cache._slot_for`` (``CACHE_BASE / version``) and
        into ``git clone --branch``. An absolute value, a ``..`` segment, or a
        leading ``-`` (flag injection) is rejected at parse time, before any path
        construction. Slash-bearing branch names (``feature/foo``) and semver
        tags (``v3.0.0``) are allowed — git refnames never contain ``..`` or a
        leading ``-``.
        """
        if v is None:
            return v
        from pathlib import Path  # noqa: PLC0415

        if Path(v).is_absolute() or ".." in Path(v).parts or v.startswith("-"):
            raise ValueError(
                f"invalid pinned_overrides version {v!r}: must not be absolute, "
                "contain a '..' segment, or start with '-' (SEC-7)"
            )
        return v


class SyncManifest(BaseModel):
    """Pydantic model for .foundry/manifest.yaml."""

    foundry_primitives_version: str
    # Optional: the git URL the CLI pulls the primitive library FROM. Omitted →
    # the canonical foundry repo (cache._DEFAULT_REPO_URL). A client fork sets
    # this to its own repository so `forge-foundry-sync` syncs from there
    # (centralized sync source, #1283). Must be a git URL, not a filesystem path.
    foundry_primitives_source: str | None = None
    project_id: str
    # FLEX-5 (#177): accept either a bare path string (legacy) or a
    # PrimitiveOverride dict (preferred for multi-library composition). Pydantic
    # tries the dict shape first; a string value falls through to the str arm.
    pinned_overrides: dict[str, PrimitiveOverride | str] = {}
    # Directory prefixes the consumer explicitly trusts as pinned_overrides roots.
    # A pinned_overrides value must resolve under the foundry cache
    # (~/.cache/forge-foundry-primitives) or under one of these prefixes; any other
    # path is rejected by the copier so a tampered manifest cannot redirect a
    # primitive at attacker-controlled content (SEC-7, #160).
    allowed_alt_roots: list[str] = Field(default_factory=list)
    # HarnessBackend (issue #435 / SPEC §13 OQ7): optional env defaults that the
    # propagator writes to `.foundry/env` so consuming SKILL.md / Copilot prompts
    # pick them up. The canonical use case is
    # ``env: {FORGE_HARNESS_NO_FALLBACK: "1"}`` for BFSI consumers that
    # forbid a second authenticated channel — making the harness-vs-bedrock
    # fallback an explicit, manifest-declared boundary instead of a default.
    # Values are stringified at propagation time; keys are validated as
    # uppercase ASCII identifiers (typical env-var shape) to keep us out of
    # the way of subtler issues like locale or shell quoting.
    env: dict[str, str] = Field(default_factory=dict)
    # Optional filter: which harness targets to emit adapters for. Defaults to
    # all five built-in logical targets. A consumer can restrict emission to a
    # subset (e.g. ``harnesses: [claude_code]``) to avoid writing adapter files
    # for harnesses not in use. Pydantic coerces a YAML list to tuple[str, ...].
    harnesses: tuple[str, ...] = Field(default=DEFAULT_HARNESSES)

    @field_validator("foundry_primitives_source")
    @classmethod
    def _validate_source_url(cls, v: str | None) -> str | None:
        """SEC-7: constrain the library source to a git URL, no flag injection.

        The value flows into ``cache.get_primitives_path_for(source_url, version)``
        → ``git clone <source_url>``. Restrict it to a git URL shape (not a bare
        filesystem path — git URL only per the centralized-sync-source design) and
        reject a leading ``-`` (a value git-clone would parse as a flag), mirroring
        ``PrimitiveOverride._reject_unsafe_version``. ``None`` (omitted) passes and
        the CLI falls back to the canonical foundry repo.
        """
        if v is None:
            return v
        if v.startswith("-"):
            raise ValueError(
                f"invalid foundry_primitives_source {v!r}: must not start with '-' "
                "(git-clone flag injection, SEC-7)"
            )
        if not v.startswith(("http://", "https://", "git@", "ssh://", "git+")):
            raise ValueError(
                f"invalid foundry_primitives_source {v!r}: must be a git URL "
                "(http://, https://, git@, ssh://, or git+…), not a filesystem path"
            )
        return v


def read_manifest(project_root: Path) -> SyncManifest:
    """Read and validate .foundry/manifest.yaml from *project_root*.

    Args:
        project_root: Root directory of the consuming project (contains `.foundry/`).

    Returns:
        A validated :class:`SyncManifest` instance.

    Raises:
        ManifestNotFound: If ``.foundry/manifest.yaml`` is absent.
        ManifestParseError: If the file is not valid YAML or fails schema validation.
    """
    manifest_path = project_root / ".foundry" / "manifest.yaml"
    if not manifest_path.is_file():
        raise ManifestNotFound(
            f".foundry/manifest.yaml not found in {project_root}. "
            "Run `forge-foundry-sync` from a project root that has a .foundry/manifest.yaml."
        )

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestParseError(f"Failed to parse {manifest_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestParseError(
            f"{manifest_path} must be a YAML mapping, got {type(raw).__name__}."
        )

    try:
        return SyncManifest(**raw)
    except (ValidationError, TypeError) as exc:
        raise ManifestParseError(
            f"{manifest_path} does not match the expected schema: {exc}"
        ) from exc

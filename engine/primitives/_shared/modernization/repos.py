"""Loader for .foundry/repos.yaml — multi-repo modernization manifest (MR1, #1550).

Mirrors the structure of ``forge_sync/manifest_reader.py``: exception classes,
a ``read_*`` function, ``yaml.safe_load``, pydantic validation, and SEC-7 path
guard (``..``-before-``.resolve()`` discipline).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from primitives._shared.modernization.models import RepoEntry, RepoSet, TwinRepoPair

if TYPE_CHECKING:
    from pathlib import Path


class RepoSetNotFound(FileNotFoundError):
    """Raised when .foundry/repos.yaml does not exist (informational only; callers return empty)."""


class RepoSetParseError(ValueError):
    """Raised when .foundry/repos.yaml is invalid YAML or fails schema validation."""


def _reject_unsafe_path(raw_path: str, label: str = "path") -> None:
    """SEC-7: reject a raw path string before any ``.resolve()`` call.

    Mirrors ``PrimitiveOverride._reject_unsafe_version`` — checks the RAW
    value so a ``..`` that would survive ``.resolve()`` normalization is still
    caught at parse time.
    """
    parts = raw_path.replace("\\", "/").split("/")
    if ".." in parts:
        raise RepoSetParseError(
            f"invalid {label} {raw_path!r}: must not contain a '..' segment (SEC-7)"
        )
    if raw_path.startswith("-"):
        raise RepoSetParseError(
            f"invalid {label} {raw_path!r}: must not start with '-' (flag injection, SEC-7)"
        )


def _validate_repo_dir(raw_path: str, project_root: Path, label: str = "path") -> None:
    """SEC-7: after raw checks, resolve relative to project_root and require is_dir()."""
    _reject_unsafe_path(raw_path, label=label)
    resolved = (project_root / raw_path).resolve()
    if not resolved.is_dir():
        raise RepoSetParseError(
            f"invalid {label} {raw_path!r}: resolved path {resolved} is not an existing directory"
        )


def read_repo_set(project_root: Path) -> RepoSet:
    """Read and validate ``.foundry/repos.yaml`` from *project_root*.

    Returns an empty :class:`RepoSet` when the file is absent — absence is the
    additive / single-repo default (invariant #1408). Only raises
    :class:`RepoSetParseError` on malformed YAML or schema violations.

    Args:
        project_root: Root directory containing ``.foundry/``.

    Returns:
        A validated :class:`RepoSet`. Empty when ``repos.yaml`` is absent.

    Raises:
        RepoSetParseError: If the file exists but is not valid YAML, fails
            schema validation, has a role/key mismatch, or any path is unsafe /
            not an existing directory.
    """
    repos_path = project_root / ".foundry" / "repos.yaml"
    if not repos_path.is_file():
        return RepoSet(entries=())

    try:
        raw = yaml.safe_load(repos_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RepoSetParseError(f"Failed to parse {repos_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RepoSetParseError(f"{repos_path} must be a YAML mapping, got {type(raw).__name__}.")

    repos_block = raw.get("repos")
    if repos_block is None:
        return RepoSet(entries=())
    if not isinstance(repos_block, dict):
        raise RepoSetParseError(
            f"{repos_path}: 'repos' must be a mapping, got {type(repos_block).__name__}."
        )

    entries: list[RepoEntry] = []
    for map_key, entry_data in repos_block.items():
        if not isinstance(entry_data, dict):
            raise RepoSetParseError(f"{repos_path}: entry for role {map_key!r} must be a mapping.")

        role_field = entry_data.get("role")
        if role_field != map_key:
            raise RepoSetParseError(
                f"{repos_path}: role field {role_field!r} does not match map key {map_key!r}."
            )

        raw_path = entry_data.get("path", "")
        _validate_repo_dir(str(raw_path), project_root, label=f"repos[{map_key!r}].path")

        try:
            entry = RepoEntry(**entry_data)
        except (ValidationError, TypeError) as exc:
            raise RepoSetParseError(
                f"{repos_path}: entry for role {map_key!r} "
                f"does not match the expected schema: {exc}"
            ) from exc

        entries.append(entry)

    return RepoSet(entries=tuple(entries))


def build_repo_set(project_root: Path, repo_overrides: tuple[str, ...]) -> RepoSet:
    """Build a :class:`RepoSet` from the manifest and ``--repo`` CLI overrides.

    Loads ``.foundry/repos.yaml`` via :func:`read_repo_set`, then applies each
    override in *repo_overrides* (``"role=path"`` strings). An override REPLACES
    the manifest entry for its role; a new role is appended. Validates each
    override path with the same SEC-7 discipline as the manifest loader.

    Args:
        project_root: Project root (used for manifest discovery and path resolution).
        repo_overrides: Sequence of ``"role=path"`` strings (from ``--repo``).

    Returns:
        The merged :class:`RepoSet`.

    Raises:
        RepoSetParseError: On schema violation or unsafe override path.
    """
    base = read_repo_set(project_root)

    if not repo_overrides:
        return base

    entries_by_role: dict[str, RepoEntry] = {e.role: e for e in base.entries}

    for token in repo_overrides:
        if "=" not in token:
            raise RepoSetParseError(f"--repo override must be 'role=path', got {token!r}")
        role, raw_path = token.split("=", 1)
        _validate_repo_dir(raw_path, project_root, label=f"--repo {role!r}")
        try:
            entry = RepoEntry(role=role, path=raw_path)  # type: ignore[arg-type]
        except (ValidationError, TypeError) as exc:
            raise RepoSetParseError(
                f"--repo override {token!r} does not match the expected schema: {exc}"
            ) from exc
        entries_by_role[role] = entry

    return RepoSet(entries=tuple(entries_by_role.values()))


# ---------------------------------------------------------------------------
# Twin-repo pairing — SEC-7 validated (D1, #1629)
# ---------------------------------------------------------------------------


def _safe_repo_path(raw: str, project_root: Path) -> str:
    """SEC-7: validate a raw twin-repo path and return the resolved absolute path.

    Mirrors :func:`_reject_unsafe_path` / :func:`_validate_repo_dir`: checks the
    RAW value before ``.resolve()`` so a ``..`` that would survive normalization is
    still caught at parse time.

    Args:
        raw: The raw path string (CLI argument value).
        project_root: Project root — relative paths are resolved against this.

    Returns:
        The resolved absolute path string (``str(resolved)``).

    Raises:
        ValueError: If ``raw`` starts with ``'-'`` (flag injection), contains a
            ``..`` segment (path traversal), or the resolved path is not an
            existing directory.
    """
    if raw.startswith("-"):
        raise ValueError(f"repo path may not start with '-': {raw!r}")
    if ".." in raw.replace("\\", "/").split("/"):
        raise ValueError(f"repo path may not contain '..': {raw!r}")
    resolved = (project_root / raw).resolve()
    if not resolved.is_dir():
        raise ValueError(f"repo path does not resolve to a directory: {raw!r}")
    return str(resolved)


def parse_twin_repo(
    legacy: str | None,
    target: str | None,
    project_root: Path,
) -> TwinRepoPair | None:
    """Parse and SEC-7-validate the twin-repo ``--legacy``/``--target`` pair (D1, #1629).

    Both arguments must be provided together (both-or-neither rule). When both
    are ``None`` the function returns ``None`` and the pipeline is byte-identical
    to today's single-repo / multi-repo behaviour (additive invariant).

    Args:
        legacy: Raw path to the legacy monolith repository (CLI ``--legacy``), or
            ``None`` when the option was not supplied.
        target: Raw path to the target (rewrite) repository (CLI ``--target``), or
            ``None`` when the option was not supplied.
        project_root: Project root used to resolve relative paths.

    Returns:
        A :class:`~primitives._shared.modernization.models.TwinRepoPair` with
        resolved absolute path strings, or ``None`` when both arguments are absent.

    Raises:
        ValueError: If only one of *legacy* / *target* is provided, or if either
            path fails the SEC-7 guard (leading ``-``, ``..`` segment, or not an
            existing directory).
    """
    if legacy is None and target is None:
        return None
    if not (legacy and target):
        raise ValueError("--legacy and --target must be provided together")
    return TwinRepoPair(
        legacy=_safe_repo_path(legacy, project_root),
        target=_safe_repo_path(target, project_root),
    )

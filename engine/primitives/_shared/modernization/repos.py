"""Loader for .foundry/repos.yaml — multi-repo modernization manifest (MR1, #1550).

Mirrors the structure of ``forge_sync/manifest_reader.py``: exception classes,
a ``read_*`` function, ``yaml.safe_load``, pydantic validation, and SEC-7 path
guard (``..``-before-``.resolve()`` discipline).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from primitives._shared.modernization.models import RepoEntry, RepoSet, TwinRepoPair


class RepoSetNotFound(FileNotFoundError):
    """Raised when .foundry/repos.yaml does not exist (informational only; callers return empty)."""


class RepoSetParseError(ValueError):
    """Raised when .foundry/repos.yaml is invalid YAML or fails schema validation."""


def _reject_flag_injection(raw_path: str, label: str = "path") -> None:
    """SEC-7: reject a raw path that would be parsed as a CLI flag.

    A leading ``-`` is rejected on the RAW value before any ``.resolve()`` /
    ``git clone --branch`` argv construction (flag injection). Unlike the copier's
    ``pinned_overrides`` guard, ``..`` is NOT blanket-rejected here — a repos.yaml
    ``../sibling`` is the idiomatic multi-repo layout; traversal is bounded instead
    by :func:`_trusted_roots` containment (#1665).
    """
    if raw_path.startswith("-"):
        raise RepoSetParseError(
            f"invalid {label} {raw_path!r}: must not start with '-' (flag injection, SEC-7)"
        )


def _trusted_roots(project_root: Path, allowed_alt_roots: tuple[str, ...] = ()) -> list[Path]:
    """The resolved prefixes a repo path may resolve under (#1665).

    A repos.yaml ``../sibling`` path is legitimate: the ui/api/db repos are
    commonly checked out side by side. So instead of blanket-rejecting ``..``
    (the copier's ``pinned_overrides`` discipline, where the threat is a
    redirected primitive-source clone), a repo path is allowed when its resolved
    location stays under a trusted prefix:

    * ``project_root.parent`` — the siblings directory (always trusted); this is
      what makes ``../sibling-repo`` work while ``../../elsewhere`` and absolute
      paths outside are still rejected.
    * any prefix the operator explicitly opts into via ``allowed_alt_roots:`` in
      repos.yaml (mirrors ``manifest.allowed_alt_roots`` in the copier, #726) —
      for repos parked outside the siblings tree.

    Returns the resolved trusted prefixes.
    """
    roots = [project_root.resolve().parent]
    roots.extend(Path(_expand_user_path(alt)).resolve() for alt in allowed_alt_roots)
    return roots


def _expand_user_path(raw_path: str) -> str:
    """Expand a leading ``~`` / ``~user`` in *raw_path* (safe post-SEC-7-raw-checks).

    A ``--repo role=path`` value is a single argv token, so the shell never expands
    ``~`` in the path portion; without this a ``~/projects/x`` is treated as a
    relative segment and joined onto the project root (``<root>/~/projects/x``),
    producing a nonsensical "not an existing directory" error (#1651). Only a
    leading ``~`` is expanded (``os.path.expanduser`` no-ops otherwise). Applied
    AFTER the ``..``/leading-``-`` SEC-7 raw checks so those guards see the raw
    value first; ``~`` cannot smuggle a ``..`` past them (expansion runs second).
    """
    return str(Path(raw_path).expanduser()) if raw_path.startswith("~") else raw_path


def _validate_repo_dir(
    raw_path: str,
    project_root: Path,
    label: str = "path",
    allowed_alt_roots: tuple[str, ...] = (),
) -> None:
    """SEC-7: reject flag injection, then resolve and require containment + is_dir().

    A ``../sibling`` path is permitted when it resolves under a trusted root
    (``project_root.parent`` or an ``allowed_alt_roots`` prefix, #1665); a path
    resolving outside every trusted root is rejected as traversal.
    """
    _reject_flag_injection(raw_path, label=label)
    resolved = (project_root / _expand_user_path(raw_path)).resolve()
    roots = _trusted_roots(project_root, allowed_alt_roots)
    if not any(resolved.is_relative_to(root) for root in roots):
        raise RepoSetParseError(
            f"invalid {label} {raw_path!r}: resolved path {resolved} escapes the trusted "
            f"roots {[str(r) for r in roots]} (traversal, SEC-7). Add its parent to "
            f"repos.yaml 'allowed_alt_roots:' to opt in."
        )
    if not resolved.is_dir():
        raise RepoSetParseError(
            f"invalid {label} {raw_path!r}: resolved path {resolved} is not an existing directory"
        )


def _parse_allowed_alt_roots(raw: dict[str, object], repos_path: Path) -> tuple[str, ...]:
    """Parse the optional top-level ``allowed_alt_roots:`` list from repos.yaml (#1665).

    Each entry is an extra trusted prefix a repo ``path`` may resolve under (in
    addition to the always-trusted ``project_root.parent``). Must be a list of
    strings; a leading ``-`` is rejected (flag injection) on the raw value.
    Absent → empty tuple (only the siblings dir is trusted).
    """
    block = raw.get("allowed_alt_roots")
    if block is None:
        return ()
    if not isinstance(block, list):
        raise RepoSetParseError(
            f"{repos_path}: 'allowed_alt_roots' must be a list, got {type(block).__name__}."
        )
    roots: list[str] = []
    for item in block:
        if not isinstance(item, str):
            raise RepoSetParseError(
                f"{repos_path}: 'allowed_alt_roots' entries must be strings, got {item!r}."
            )
        if item.startswith("-"):
            raise RepoSetParseError(
                f"{repos_path}: allowed_alt_roots entry {item!r} must not start with '-'."
            )
        roots.append(item)
    return tuple(roots)


def _alt_roots_from_manifest(project_root: Path) -> tuple[str, ...]:
    """Best-effort read of ``allowed_alt_roots:`` for --repo override validation (#1665).

    Returns the manifest's trusted alt-roots (or empty when no/invalid manifest);
    a ``--repo`` override may resolve under the same operator-opted-in prefixes as
    a manifest path. Never raises — a malformed manifest yields no extra roots (the
    override then falls back to the siblings-only default).
    """
    repos_path = project_root / ".foundry" / "repos.yaml"
    if not repos_path.is_file():
        return ()
    try:
        raw = yaml.safe_load(repos_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return _parse_allowed_alt_roots(raw, repos_path)
    except (yaml.YAMLError, RepoSetParseError, OSError):
        return ()
    return ()


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

    allowed_alt_roots = _parse_allowed_alt_roots(raw, repos_path)

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
        _validate_repo_dir(
            str(raw_path),
            project_root,
            label=f"repos[{map_key!r}].path",
            allowed_alt_roots=allowed_alt_roots,
        )

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

    # An override's ``../sibling`` gets the same trusted-root containment as a
    # manifest path, honoring any repos.yaml ``allowed_alt_roots:`` (#1665).
    override_alt_roots = _alt_roots_from_manifest(project_root)

    entries_by_role: dict[str, RepoEntry] = {e.role: e for e in base.entries}

    for token in repo_overrides:
        if "=" not in token:
            raise RepoSetParseError(f"--repo override must be 'role=path', got {token!r}")
        role, raw_path = token.split("=", 1)
        _validate_repo_dir(
            raw_path, project_root, label=f"--repo {role!r}", allowed_alt_roots=override_alt_roots
        )
        # Store the ~-expanded path so downstream consumers resolve the real dir,
        # not a literal '~' segment (#1651). SEC-7 raw checks already ran above.
        try:
            entry = RepoEntry(role=role, path=_expand_user_path(raw_path))  # type: ignore[arg-type]
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

    Mirrors :func:`_validate_repo_dir`: rejects a leading ``-`` on the raw value,
    then resolves and requires the result to stay under a trusted root (a
    ``../sibling`` twin is allowed; a path escaping the siblings root is rejected
    as traversal, #1665).

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
    # Expand a leading ~ AFTER the raw leading-'-' check (#1651) so ~/path resolves
    # to the user's home rather than a literal '~' segment under project_root.
    resolved = (project_root / _expand_user_path(raw)).resolve()
    # A twin legacy/rewrite pair is commonly checked out beside the project (#1665):
    # allow ``../sibling`` when it stays under the trusted siblings root; a path that
    # escapes it is rejected as traversal.
    roots = _trusted_roots(project_root)
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError(
            f"repo path {raw!r} resolves to {resolved}, which escapes the trusted roots "
            f"{[str(r) for r in roots]} (traversal, SEC-7)"
        )
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

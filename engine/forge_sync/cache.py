"""Primitive library cache: fetch/checkout by git tag.

Vendored from forge-foundry-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

# CHORE-2 (#153): cache base is overridable via FORGE_FOUNDRY_CACHE_BASE so
# tests / CI / containerised environments can redirect away from ``~/.cache``
# without monkeypatching. Read once at import: any later runtime change in the
# env doesn't take effect without re-importing the module, which is consistent
# with the rest of the package's import-time configuration.
_CACHE_ENV_VAR = "FORGE_FOUNDRY_CACHE_BASE"
CACHE_BASE: Path = (
    Path(os.environ[_CACHE_ENV_VAR])
    if os.environ.get(_CACHE_ENV_VAR)
    else Path.home() / ".cache" / "forge-foundry-primitives"
)
_DEFAULT_REPO_URL: str = "https://github.com/arula-ai/forge-foundry-primitives.git"


class CacheError(RuntimeError):
    """Raised when the cache cannot be populated (bad version, git failure, etc.)."""


def _clone_tag(version: str, dest: Path, *, source_url: str = _DEFAULT_REPO_URL) -> None:
    """Shallow-clone *source_url* at *version* into *dest*."""
    result = subprocess.run(  # noqa: S603
        ["git", "clone", "--depth", "1", "--branch", version, source_url, str(dest)],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise CacheError(
            f"git clone failed for {source_url}@{version!r} "
            f"(exit {result.returncode}):\n{result.stderr}"
        )


def default_repo_url() -> str:
    """The canonical foundry library repo URL — the source used when a manifest
    omits ``foundry_primitives_source`` (#1284). Public accessor so callers need
    not reach the private ``_DEFAULT_REPO_URL`` name."""
    return _DEFAULT_REPO_URL


def get_primitives_path(version: str) -> Path:
    """Foundry-library shim — fetch the canonical foundry repo at *version*.

    Equivalent to ``get_primitives_path_for(DEFAULT_REPO_URL, version)`` and
    kept as the legacy single-library entry point.
    """
    return get_primitives_path_for(_DEFAULT_REPO_URL, version)


def _slot_for(source_url: str, version: str) -> Path:
    """Cache slot for *(source_url, version)* — collision-safe across libraries.

    The default foundry URL uses the legacy ``<version>/`` layout so existing
    caches stay valid; any other URL gets a hash-prefixed slot.

    SEC-7 defense-in-depth: *version* originates from ``PrimitiveOverride.version``
    in the manifest. A value like ``../../../tmp/evil`` (or an absolute path) would
    place the slot — and the ``git clone`` that follows — outside :data:`CACHE_BASE`,
    before the copier's ``_is_within_allowed_roots`` check can fire. Reject traversal
    on the **raw** value here, mirroring ``copier.py::_resolve_source``.
    """
    _reject_unsafe_version(version)
    if source_url == _DEFAULT_REPO_URL:
        return CACHE_BASE / version
    digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:12]
    return CACHE_BASE / f"{digest}-{version}"


def _reject_unsafe_version(version: str) -> None:
    """Raise ``ValueError`` if *version* could escape :data:`CACHE_BASE` (SEC-7).

    Checks the raw string *before* any ``Path`` construction: an absolute value,
    a ``..`` segment, or a leading ``-`` (which ``git clone --branch`` would parse
    as a flag) is rejected. Legitimate semver tags (``v3.0.0``) and slash-bearing
    branch names (``feature/foo``) pass — git refnames never contain ``..`` or a
    leading ``-`` either.
    """
    parts = Path(version).parts
    if Path(version).is_absolute() or ".." in parts or version.startswith("-"):
        raise ValueError(
            f"version contains a path-traversal or flag-injection segment: "
            f"{version!r} — must not be absolute, contain a '..' segment, or "
            "start with '-' (SEC-7)"
        )


def get_primitives_path_for(source_url: str, version: str) -> Path:
    """Return a local path to the primitives library at *(source_url, version)*.

    FLEX-5 (#177) generalisation of :func:`get_primitives_path` — accepts any
    git URL so a consumer can compose primitives from multiple libraries
    (foundry@v1, a third-party fork@v0, …) by listing each in
    ``pinned_overrides`` as a ``PrimitiveOverride(source=…, version=…)`` entry.

    Args:
        source_url: HTTPS or SSH git URL of the source repository.
        version: Semver-style tag or branch name to check out.

    Returns:
        ``Path`` to the cached clone directory (always under :data:`CACHE_BASE`,
        so the SEC-7 allowlist accepts it without additional opt-in).

    Raises:
        CacheError: If *version* is empty/whitespace or the git clone fails.
    """
    if not version or not version.strip():
        raise CacheError(f"version must be a non-empty string, got {version!r}")

    CACHE_BASE.mkdir(parents=True, exist_ok=True)
    dest = _slot_for(source_url, version)

    if dest.exists():
        return dest

    # Atomic lock: mkdir() is atomic on POSIX — if another process is already
    # cloning, our mkdir() raises FileExistsError. We wait for the clone to
    # finish (dest appears) or the lock to be released (stale lock cleanup).
    lock = dest.parent / f"{dest.name}.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        # Another process is cloning. Wait for dest to appear.
        for _ in range(60):
            if dest.exists():
                return dest
            time.sleep(1)
        raise CacheError(
            f"Timed out waiting for concurrent clone of {source_url}@{version!r}"
        ) from None

    try:
        _clone_tag(version, dest, source_url=source_url)
    finally:
        lock.rmdir()

    return dest

"""Primitive copier: copies primitives into a project's .foundry/primitives/.

Vendored from forge-foundry-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.

Design decisions:
  - Missing primitives (not found in the library) emit a WARNING and are skipped,
    not raised as errors, so a partial update never breaks the whole sync.
  - pinned_overrides maps a primitive name to an *alternate library root path string*
    (not a version tag). The caller (e.g. forge_sync.cli) is responsible for
    resolving version tags to local paths via forge_sync.cache before invoking
    copy_primitives. This keeps the copier stateless and easily testable.
  - Each primitive destination is wiped (rmtree) then re-created for a clean copy,
    which prevents stale files from lingering across syncs.
  - .git/ directories inside a primitive source are always excluded.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from forge_sync.cache import CACHE_BASE, get_primitives_path_for
from forge_sync.manifest_reader import PrimitiveOverride

if TYPE_CHECKING:
    from forge_sync.manifest_reader import SyncManifest

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ignore_git(_directory: str, contents: list[str]) -> list[str]:
    """shutil.copytree ignore callback — always exclude .git/ directories."""
    return [".git"] if ".git" in contents else []


def _ignore_git_and_symlinks(directory: str, contents: list[str]) -> set[str]:
    """``shutil.copytree`` ignore callback — exclude ``.git/`` AND symlinks (SEC-10).

    ``copy_primitives`` copies library sources into the consumer's
    ``.foundry/primitives/`` tree. Without this, a symlinked file under a
    primitive source would have its *target* content materialized into the
    consumer tree (arbitrary host-file read), mirroring the exfil vector the
    emitter loader and the propagator copy helpers already close. A symlinked
    *directory* is likewise skipped so its tree is not traversed. The library
    source is usually the repo or a shallow clone (symlinks rarely survive a
    clone), but skipping them keeps SEC-10's defense-in-depth uniform.
    """
    ignored: set[str] = set(_ignore_git(directory, contents))
    base = Path(directory)
    for name in contents:
        if (base / name).is_symlink():
            ignored.add(name)
    return ignored


def _resolve_override(primitive_name: str, raw: object) -> Path:
    """Resolve a ``pinned_overrides`` entry to a library root path.

    Accepts either the legacy bare string (a path) or a
    :class:`PrimitiveOverride` instance (FLEX-5, #177). For URL-shaped sources,
    the cache is consulted via :func:`forge_sync.cache.get_primitives_path_for`,
    which always returns a path under :data:`CACHE_BASE` (so SEC-7's allowlist
    accepts it without further opt-in). For path-shaped sources, the SEC-7
    ``..``-on-raw guard fires before ``.resolve()`` collapses anything.
    """
    if isinstance(raw, PrimitiveOverride):
        source = raw.source
        if source.startswith(("http://", "https://", "git@", "ssh://", "git+")):
            if not raw.version:
                raise ValueError(
                    f"pinned_overrides for {primitive_name!r} has a URL source but no version"
                )
            return get_primitives_path_for(source, raw.version)
        # Filesystem path shape inside a PrimitiveOverride.
        path_str = source
    elif isinstance(raw, str):
        # Legacy shape: the value IS the filesystem path.
        path_str = raw
    else:
        raise TypeError(
            f"pinned_overrides for {primitive_name!r} has unexpected type {type(raw).__name__}"
        )
    if ".." in Path(path_str).parts:
        raise ValueError(
            f"pinned_overrides for {primitive_name!r} contains path traversal: {path_str!r}"
        )
    return Path(path_str).resolve()


def _is_within_allowed_roots(alt_root: Path, manifest: SyncManifest) -> bool:
    """True if *alt_root* lives under the foundry cache or an opted-in root.

    Both *alt_root* and each candidate prefix are resolved so the lexical
    comparison survives symlinks (e.g. ``/tmp`` → ``/private/tmp`` on macOS).
    """
    allowed = [CACHE_BASE.resolve(), *(Path(p).resolve() for p in manifest.allowed_alt_roots)]
    return any(alt_root.is_relative_to(prefix) for prefix in allowed)


def _resolve_source(
    library_root: Path,
    primitive_name: str,
    manifest: SyncManifest,
) -> Path:
    """Return the source primitives/<name> directory for *primitive_name*.

    If *primitive_name* appears in ``manifest.pinned_overrides``, the value is
    treated as an alternate library root path and that library's copy of the
    primitive is used instead of *library_root*.

    The override is constrained two ways so a tampered manifest cannot redirect a
    primitive at attacker-controlled content (SEC-7, #160):

    * ``..`` segments are rejected on the **raw** value — ``.resolve()`` collapses
      them, so the guard must run before resolving.
    * The resolved path must live under the foundry cache or a prefix the consumer
      explicitly opted into via ``manifest.allowed_alt_roots``.
    """
    if primitive_name in manifest.pinned_overrides:
        alt_root = _resolve_override(primitive_name, manifest.pinned_overrides[primitive_name])
        if not _is_within_allowed_roots(alt_root, manifest):
            raise ValueError(
                f"pinned_overrides for {primitive_name!r} points outside the allowed roots: "
                f"{alt_root}. Permitted roots are the foundry cache ({CACHE_BASE}) and any "
                f"prefix listed in the manifest's allowed_alt_roots (currently "
                f"{manifest.allowed_alt_roots})."
            )
        return alt_root / "primitives" / primitive_name
    return library_root / "primitives" / primitive_name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def copy_primitives(
    library_root: Path,
    project_root: Path,
    manifest: SyncManifest,
    primitive_names: list[str] | None = None,
) -> list[str]:
    """Copy primitives from *library_root* into *project_root*/.foundry/primitives/.

    Args:
        library_root: Root of the primitives library (contains ``primitives/``).
        project_root: Root of the target consuming project.
        manifest: Parsed :class:`~forge_sync.manifest_reader.SyncManifest`.
        primitive_names: Explicit list of primitive names to copy.  When ``None``
            all primitives found under ``library_root/primitives/`` are copied
            (subject to *pinned_overrides*).

    Returns:
        Sorted list of primitive names that were successfully copied.

    Notes:
        - Primitives absent from the library emit a WARNING and are skipped.
        - The destination for each primitive is wiped before copying (clean copy).
        - ``.git/`` directories are never copied.
        - Calling this function twice with identical inputs is idempotent.
    """
    primitives_src_root = library_root / "primitives"
    dest_root = project_root / ".foundry" / "primitives"
    dest_root.mkdir(parents=True, exist_ok=True)

    # Determine which primitives to process.
    if primitive_names is None:
        if primitives_src_root.is_dir():
            names = sorted(p.name for p in primitives_src_root.iterdir() if p.is_dir())
        else:
            names = []
    else:
        names = list(primitive_names)

    copied: list[str] = []
    for name in names:
        src = _resolve_source(library_root, name, manifest)
        if not src.is_dir():
            logger.warning(
                "Primitive %r not found in library at %s — skipping.",
                name,
                src,
            )
            continue

        dest = dest_root / name

        # Clean copy: wipe existing destination first.
        if dest.exists():
            shutil.rmtree(dest)

        shutil.copytree(src, dest, ignore=_ignore_git_and_symlinks)
        copied.append(name)
        logger.debug("Copied primitive %r → %s", name, dest)

    # Copy the top-level ``primitives/*.py`` support modules (#1347). The loop
    # above copies each primitive *directory* (and ``_shared/``), but the
    # library also carries package-level helpers directly under ``primitives/``
    # — ``__init__.py`` (which makes the synced tree an importable package) and
    # cross-primitive support modules such as ``_response_schema_validator.py``,
    # ``_renderer_registry.py``, ``_xml_safety.py`` and ``_tool_version.py``.
    # Several synced ``shared/models.py`` modules import these, so omitting them
    # left the synced tree import-incomplete (``ModuleNotFoundError``). Copy
    # every regular ``*.py`` file at the ``primitives/`` root, skipping symlinks
    # (SEC-10 discipline, matching ``_ignore_git_and_symlinks``).
    if primitives_src_root.is_dir():
        for py_file in sorted(primitives_src_root.glob("*.py")):
            if py_file.is_symlink() or not py_file.is_file():
                continue
            shutil.copy2(py_file, dest_root / py_file.name)
            logger.debug("Copied support module %r → %s", py_file.name, dest_root)

    return copied

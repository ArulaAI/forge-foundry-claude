"""Idempotency helper for forge-foundry-sync.

Vendored from forge-prowess-primitives@v0.4.0 — see CHANGELOG-VENDOR.md for divergence.

Original docstring:

Computes a deterministic SHA-256 hash of the *current* state of a lab's
``.forge/`` subtree — specifically the manifest + the sorted listing of
``.foundry/primitives/`` subdirectory names.  A cache caller can store this
hash after a successful sync and skip the next run when the hash is unchanged.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def compute_project_state_hash(project_root: Path) -> str:
    """Return a SHA-256 hex digest that captures the lab's sync-relevant state.

    The digest is computed from:

    1. The **content** of ``.foundry/manifest.yaml``.
    2. The **sorted names** of immediate subdirectories under
       ``.foundry/primitives/`` (directory names only, not file contents, because
       the copier wipes and re-creates each primitive on every sync).

    Args:
        project_root: Root of the consuming project (must contain ``.foundry/manifest.yaml``).

    Returns:
        64-character lowercase hexadecimal SHA-256 digest.

    Raises:
        FileNotFoundError: If ``.foundry/manifest.yaml`` does not exist.
    """
    manifest_path = project_root / ".foundry" / "manifest.yaml"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f".foundry/manifest.yaml not found in {project_root}. "
            "Cannot compute state hash without a manifest."
        )

    # NOTE: the hash intentionally covers only (a) manifest bytes and (b) the
    # *names* of primitive subdirectories — not their file contents.  Editing a
    # primitive's source files without adding or removing directories will NOT
    # change the hash, so callers that skip a sync on an unchanged hash may miss
    # in-place edits.  This is by design: the copier wipes and re-creates each
    # primitive on every sync, so changed file contents always result from a
    # changed manifest (version bump or pinned_override) rather than from a
    # stale dir listing.  If you add a content-level staleness check, hash the
    # primitive file tree here instead of just the dir names.
    h = hashlib.sha256()

    # 1. Hash manifest content.
    h.update(manifest_path.read_bytes())

    # 2. Hash sorted primitive subdir listing (names only — not file contents).
    primitives_root = project_root / ".foundry" / "primitives"
    if primitives_root.is_dir():
        names = sorted(p.name for p in primitives_root.iterdir() if p.is_dir())
    else:
        names = []

    for name in names:
        h.update(name.encode())

    return h.hexdigest()

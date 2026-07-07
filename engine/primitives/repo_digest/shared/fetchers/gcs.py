"""Google Cloud Storage fetcher — stub for v0.4.0.

Planned for v0.5.0: download `.foundry/context/` from a GCS bucket path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import FetchOptions


def fetch_peer_gcs(peer_repo: str, *, options: FetchOptions, cache_dir: Path) -> Path:
    raise NotImplementedError("Backend 'gcs' planned for v0.5.0+; use 'git' for now.")

"""Fetcher dispatcher — routes peer fetches by backend name."""

from __future__ import annotations

from typing import TYPE_CHECKING

from primitives.repo_digest.shared.fetchers.azure_blob import fetch_peer_azure_blob
from primitives.repo_digest.shared.fetchers.gcs import fetch_peer_gcs
from primitives.repo_digest.shared.fetchers.gh_api import fetch_peer_gh_api
from primitives.repo_digest.shared.fetchers.git import fetch_peer_git
from primitives.repo_digest.shared.fetchers.s3 import fetch_peer_s3

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import FetchOptions

_SUPPORTED_BACKENDS = ("git", "gh-api", "s3", "azure-blob", "gcs")


def fetch_peer(
    peer_repo: str,
    backend: str,
    *,
    options: FetchOptions,
    cache_dir: Path,
    allowed_hosts: frozenset[str] = frozenset(),
) -> Path:
    """Dispatch to the right backend; raise on unknown backend.

    ``allowed_hosts`` is forwarded to the git backend for URL-policy enforcement
    (see issue #82). Stub backends ignore it harmlessly.

    Uses structural pattern matching so mypy narrows each arm and `git`'s
    extra ``allowed_hosts`` kwarg type-checks cleanly (#109 / MOD-3).
    """
    match backend:
        case "git":
            return fetch_peer_git(
                peer_repo,
                options=options,
                cache_dir=cache_dir,
                allowed_hosts=allowed_hosts,
            )
        case "gh-api":
            return fetch_peer_gh_api(peer_repo, options=options, cache_dir=cache_dir)
        case "s3":
            return fetch_peer_s3(peer_repo, options=options, cache_dir=cache_dir)
        case "azure-blob":
            return fetch_peer_azure_blob(peer_repo, options=options, cache_dir=cache_dir)
        case "gcs":
            return fetch_peer_gcs(peer_repo, options=options, cache_dir=cache_dir)
        case _:
            raise ValueError(
                f"Unknown fetch backend: {backend!r}. Supported: {sorted(_SUPPORTED_BACKENDS)}."
            )


__all__ = ["fetch_peer"]

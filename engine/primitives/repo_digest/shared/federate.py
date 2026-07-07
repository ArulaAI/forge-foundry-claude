"""Federation composer — fetches peer-repo digests per .foundry/platform.yaml."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from primitives.repo_digest.shared.config import (
    DigestConfig,
    PlatformConfig,
)
from primitives.repo_digest.shared.fetchers import fetch_peer

if TYPE_CHECKING:
    from pathlib import Path

_logger = logging.getLogger(__name__)

DEFAULT_PARALLELISM = 5


def _is_fresh(ttl_file: Path, ttl_minutes: int) -> bool:
    if not ttl_file.is_file():
        return False
    try:
        last = int(ttl_file.read_text().strip())
    except (OSError, ValueError):
        return False
    return (int(time.time()) - last) < ttl_minutes * 60


def _record_fetch(ttl_file: Path) -> None:
    ttl_file.parent.mkdir(parents=True, exist_ok=True)
    ttl_file.write_text(str(int(time.time())))


async def _fetch_one_peer(
    peer: str,
    platform: PlatformConfig,
    cache_dir: Path,
    ttl_minutes: int,
    semaphore: asyncio.Semaphore,
) -> tuple[str, list[Path]] | None:
    """Fetch a single peer's digest, returning (peer_name, file_list).

    Returns ``None`` when the peer is skipped (unsupported backend /
    clone failure) — matches the pre-async behaviour where skipped peers
    were absent from the result dict, not present with an empty list.
    """
    sanitized = peer.replace("/", "_")
    peer_root = cache_dir / sanitized
    ttl_file = peer_root / ".last-fetched"
    digest_dir = peer_root / ".foundry" / "context"

    if _is_fresh(ttl_file, ttl_minutes) and digest_dir.is_dir():
        return peer, sorted(digest_dir.glob("*.md"))

    async with semaphore:
        try:
            fetched = await asyncio.to_thread(
                fetch_peer,
                peer,
                platform.fetch_backend,
                options=platform.fetch_options,
                cache_dir=cache_dir,
                allowed_hosts=frozenset(platform.allowed_hosts),
            )
        except NotImplementedError as exc:
            _logger.warning("Skipping peer %s (backend not implemented): %s", peer, exc)
            return None
        except RuntimeError as exc:
            _logger.warning("Skipping peer %s (fetch failed): %s", peer, exc)
            return None

    _record_fetch(ttl_file)
    files = sorted(fetched.glob("*.md")) if fetched.is_dir() else []
    return peer, files


async def federate_peers_async(
    project_root: Path,
    *,
    config: DigestConfig | None = None,
    parallelism: int = DEFAULT_PARALLELISM,
) -> dict[str, list[Path]]:
    """Async version of :func:`federate_peers`.

    Fetches all peers concurrently with a bounded semaphore so a 10-peer
    platform doesn't spawn 10 simultaneous ``git clone`` processes.
    The bound (``parallelism``) defaults to 5 — well under the typical
    file-descriptor limit and gentle on git remotes.

    See `federate_peers` for the synchronous facade and the contract.
    """
    if config is None:
        config = DigestConfig()
    project_root = project_root.resolve()
    platform = PlatformConfig.from_project(project_root)
    if platform is None:
        return {}

    cache_dir = project_root / ".foundry" / "context" / "peers"
    cache_dir.mkdir(parents=True, exist_ok=True)

    semaphore = asyncio.Semaphore(max(1, parallelism))
    pairs = await asyncio.gather(
        *(
            _fetch_one_peer(
                peer,
                platform,
                cache_dir,
                config.federation.cache_ttl_minutes,
                semaphore,
            )
            for peer in platform.member_repos
        )
    )
    return dict(pair for pair in pairs if pair is not None)


def federate_peers(
    project_root: Path,
    *,
    config: DigestConfig | None = None,
    parallelism: int = DEFAULT_PARALLELISM,
) -> dict[str, list[Path]]:
    """Fetch peer-repo digests per .foundry/platform.yaml.

    Returns dict mapping peer-repo-name → list of digest file paths
    under <project_root>/.foundry/context/peers/<peer-name>/.

    Sync facade around :func:`federate_peers_async`. Pulls up to
    *parallelism* peers concurrently (default 5) via ``asyncio.gather`` —
    a 10-peer platform finishes in roughly the time of a single clone
    instead of the wall-clock sum. Stub backends (gh-api/s3/azure-blob/gcs)
    raise NotImplementedError — caught, logged, peer skipped.
    """
    return asyncio.run(
        federate_peers_async(
            project_root,
            config=config,
            parallelism=parallelism,
        )
    )

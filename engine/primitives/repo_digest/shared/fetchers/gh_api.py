"""GitHub API fetcher — stub for v0.4.0.

Planned for v0.5.0: use gh CLI or GitHub REST/GraphQL API to download the
`.foundry/context/` tree from a peer repo's `_foundry-context` branch via the
contents API. Avoids needing git clone access (useful when only API token is
available, e.g., on managed CI runners).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import FetchOptions


def fetch_peer_gh_api(peer_repo: str, *, options: FetchOptions, cache_dir: Path) -> Path:
    raise NotImplementedError("Backend 'gh-api' planned for v0.5.0+; use 'git' for now.")

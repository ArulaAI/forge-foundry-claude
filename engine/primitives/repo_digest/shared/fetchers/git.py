"""Git fetcher — clones a peer repo's digest branch via git sparse-checkout.

Defense-in-depth note: ``fetch_peer_git`` re-validates ``peer_repo`` against the
federation URL policy via :func:`primitives.repo_digest.shared.url_policy.validate_peer_url`
even though :class:`primitives.repo_digest.shared.config.PlatformConfig` also
validates at YAML-load time. This guards against direct callers (tests, future
code paths) that skip the config layer. See issue #82 / SEC-1.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from primitives.repo_digest.shared.url_policy import resolve_clone_url, validate_peer_url

if TYPE_CHECKING:
    from pathlib import Path

    from primitives.repo_digest.shared.config import FetchOptions


def fetch_peer_git(
    peer_repo: str,
    *,
    options: FetchOptions,
    cache_dir: Path,
    allowed_hosts: frozenset[str] = frozenset(),
    allow_local_paths: bool = False,
) -> Path:
    """Clone peer's `.foundry/context/` from a branch via git.

    Strategy:
        1. Validate ``peer_repo`` against the federation URL policy.
        2. Compute target = cache_dir / peer_repo.replace('/', '_')
        3. If exists, remove (we want fresh).
        4. git clone --depth 1 --branch <branch> --no-checkout <url> <target>
        5. cd <target>; git sparse-checkout init --cone; git sparse-checkout set .foundry/context;
           git checkout
        6. Return target / '.foundry' / 'context'.

    ``allowed_hosts`` controls which hostnames may appear in fully-qualified
    HTTPS and SSH URLs. ``allow_local_paths`` opts into ``file://`` URLs (tests).
    """
    validate_peer_url(peer_repo, allowed_hosts=allowed_hosts, allow_local_paths=allow_local_paths)

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / peer_repo.replace("/", "_")
    if target.exists():
        shutil.rmtree(target)

    url = resolve_clone_url(peer_repo)
    branch = options.branch

    if options.shallow:
        clone_cmd = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--no-checkout",
            "--",
            url,
            str(target),
        ]
        result = subprocess.run(clone_cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if result.returncode != 0:
            raise RuntimeError(
                f"git clone failed for {peer_repo} (branch {branch}): {result.stderr}"
            )
        for cmd in (
            ["git", "-C", str(target), "sparse-checkout", "init", "--cone"],
            ["git", "-C", str(target), "sparse-checkout", "set", ".foundry/context"],
            ["git", "-C", str(target), "checkout"],
        ):
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
            if r.returncode != 0:
                raise RuntimeError(
                    f"git sparse-checkout step failed for {peer_repo}: {' '.join(cmd)} → {r.stderr}"
                )
    else:
        clone_cmd_full = [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            branch,
            "--",
            url,
            str(target),
        ]
        result = subprocess.run(clone_cmd_full, capture_output=True, text=True, check=False)  # noqa: S603
        if result.returncode != 0:
            raise RuntimeError(
                f"git clone failed for {peer_repo} (branch {branch}): {result.stderr}"
            )

    return target / ".foundry" / "context"

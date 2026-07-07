"""Peer-repo URL validation policy for federation.

Prevents SSRF / arbitrary-clone attacks that originate from an attacker who
controls `.foundry/platform.yaml` in a consuming repo. See issue #82 (SEC-1).

Acceptable peer_repo forms:

* Plain GitHub slug ``org/name`` — always permitted, clones github.com.
* HTTPS URL ``https://<host>/...`` — host must appear in ``allowed_hosts``.
* SSH URL ``git@<host>:<path>`` — host must appear in ``allowed_hosts``.
* ``file://`` URL — permitted only when ``allow_local_paths=True``
  (test/dev usage; never passed when validating user-supplied platform.yaml).

Anything else (``ftp://``, raw paths, empty strings, mixed-form URLs) is rejected.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SSH_URL_RE = re.compile(r"^git@(?P<host>[A-Za-z0-9_.-]+):(?P<path>[A-Za-z0-9_./~-]+)$")
_NETWORK_SCHEMES = frozenset({"http", "https"})


class PeerUrlPolicyError(ValueError):
    """Raised when a peer URL violates the federation security policy."""


def validate_peer_url(
    peer_repo: str,
    *,
    allowed_hosts: frozenset[str] = frozenset(),
    allow_local_paths: bool = False,
) -> None:
    """Validate *peer_repo* against the federation security policy.

    Raises ``PeerUrlPolicyError`` on anything that does not match an
    explicitly-permitted form.
    """
    if not isinstance(peer_repo, str) or not peer_repo:
        raise PeerUrlPolicyError(f"peer_repo must be a non-empty string, got {peer_repo!r}")

    if _SLUG_RE.fullmatch(peer_repo):
        return

    ssh_match = _SSH_URL_RE.fullmatch(peer_repo)
    if ssh_match:
        host = ssh_match.group("host")
        if host not in allowed_hosts:
            raise PeerUrlPolicyError(
                f"SSH peer URL host {host!r} is not allowlisted "
                f"(peer_repo={peer_repo!r}); add it to PlatformConfig.allowed_hosts."
            )
        return

    # Reject ambiguous strings that look like neither slug nor SSH and have no scheme.
    if "://" not in peer_repo:
        raise PeerUrlPolicyError(
            f"peer_repo {peer_repo!r} is neither a plain 'org/name' slug, "
            "a 'git@host:path' SSH URL, nor a scheme-qualified URL."
        )

    parsed = urlparse(peer_repo)

    if parsed.scheme == "file":
        if not allow_local_paths:
            raise PeerUrlPolicyError(
                f"file:// peer URLs are not permitted in federation config: {peer_repo!r}"
            )
        return

    if parsed.scheme not in _NETWORK_SCHEMES:
        raise PeerUrlPolicyError(
            f"Unsupported URL scheme {parsed.scheme!r} in peer_repo={peer_repo!r}; "
            f"allowed schemes: {sorted(_NETWORK_SCHEMES)}."
        )

    host = parsed.hostname
    if not host:
        raise PeerUrlPolicyError(f"peer_repo URL {peer_repo!r} has no hostname")

    if host not in allowed_hosts:
        raise PeerUrlPolicyError(
            f"Peer URL host {host!r} is not allowlisted "
            f"(peer_repo={peer_repo!r}); add it to PlatformConfig.allowed_hosts."
        )


def resolve_clone_url(peer_repo: str) -> str:
    """Return the URL that ``git clone`` should be invoked with.

    Plain slugs ``org/name`` resolve to ``https://github.com/org/name.git``.
    Everything else passes through unchanged. Caller MUST have invoked
    :func:`validate_peer_url` first; this function does no validation.
    """
    if _SLUG_RE.fullmatch(peer_repo):
        return f"https://github.com/{peer_repo}.git"
    return peer_repo


__all__ = ["PeerUrlPolicyError", "resolve_clone_url", "validate_peer_url"]

"""Endpoint-level completeness assessor (#1628 D3b).

Task 4: :func:`is_stubbed` — language-agnostic structural stub check.
Task 5: :func:`assess_delta` — derives a :class:`MigrationDelta` by classifying
every legacy endpoint/entity as *present-complete* or *absent*.

Design note
-----------
``assess_delta`` is **digest-only**: it classifies match-vs-absent using the
normalized interface keys from :class:`ModernizationDigest`.  Full stub-detection
(``is_stubbed``) and ``file:line`` evidence are wired at the pipeline layer (D4
/#1630) where handler bodies and discovery citations are available.

INVARIANT (no-silent-drop): every legacy endpoint and every legacy entity appears
exactly once in the returned :class:`MigrationDelta`, either as
``"present-complete"`` (matched) or ``"absent"`` (unmatched).

QUAL-2: imports only stdlib, pydantic, and ``_shared/modernization``; no LLM.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from primitives._shared.discovery.models.entities import Confidence  # noqa: TC001
from primitives._shared.modernization.delta import (
    ContextDelta,
    Disposition,
    EndpointDelta,
    EntityDelta,
    MigrationDelta,
)
from primitives._shared.modernization.same_app_correlator import normalize_iface

if TYPE_CHECKING:
    from primitives._shared.modernization.delta import ContextPairing
    from primitives._shared.modernization.models import ModernizationDigest


# ---------------------------------------------------------------------------
# Task 4: Structural stub detector
# ---------------------------------------------------------------------------

_NOT_IMPL_RE = re.compile(
    r"NotImplemented"
    r"|UnsupportedOperationException"
    r"|NotImplementedException"
    r"|raise\s+NotImplementedError"
    r"|\bTODO\b"
    r"|\bFIXME\b"
    r"|status\(?\s*501"
    r"|HttpStatus\.NOT_IMPLEMENTED"
    r"|@Deprecated"
    r"|@stub",
    re.IGNORECASE,
)

# Strip single-line comments (// …, # …) and block comments (/* … */), then
# count remaining non-whitespace tokens.  A body with ≤2 tokens is trivially
# small enough to classify as a stub.
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/|#[^\n]*", re.DOTALL)
_STUB_TOKEN_THRESHOLD = 2


def is_stubbed(handler_body: str, flavor: str) -> bool:  # noqa: ARG001  (flavor reserved for future per-lang overrides)
    """Return ``True`` when *handler_body* looks like an unimplemented stub.

    Checks (applied in order):

    1. Empty / whitespace-only body.
    2. Any marker from the cross-language stub vocabulary:
       ``NotImplemented`` / ``UnsupportedOperationException`` /
       ``NotImplementedException`` / ``raise NotImplementedError`` /
       ``TODO`` / ``FIXME`` / ``status(501)`` / ``HttpStatus.NOT_IMPLEMENTED`` /
       ``@Deprecated`` / ``@stub``.
    3. Trivial-size: after stripping comments and splitting on non-word chars,
       ≤2 meaningful tokens remain.

    *flavor* is accepted for API compatibility and reserved for future
    language-specific overrides; it is not used in the current implementation.
    """
    body = (handler_body or "").strip()
    if not body:
        return True

    if _NOT_IMPL_RE.search(body):
        return True

    # Trivial-size check: strip comments then count meaningful tokens.
    stripped = _COMMENT_RE.sub("", body)
    tokens = [t for t in re.split(r"\W+", stripped) if t]
    return len(tokens) <= _STUB_TOKEN_THRESHOLD


# ---------------------------------------------------------------------------
# Task 5: Endpoint-level assess_delta
# ---------------------------------------------------------------------------


def _ifaces_for_context(
    digests: list[ModernizationDigest],
    context: str,
) -> list[str]:
    """Collect exposed_interfaces from all digests whose domains include *context*."""
    out: list[str] = []
    for d in digests:
        labels = d.domains if d.domains else [d.code_root]
        if context in labels:
            out.extend(d.exposed_interfaces)
    return out


def _entities_for_context(
    digests: list[ModernizationDigest],
    context: str,
) -> list[str]:
    """Collect key_entities from all digests whose domains include *context*."""
    out: list[str] = []
    for d in digests:
        labels = d.domains if d.domains else [d.code_root]
        if context in labels:
            out.extend(d.key_entities)
    return out


def assess_delta(
    pairings: list[ContextPairing],
    legacy_digests: list[ModernizationDigest],
    target_digests: list[ModernizationDigest],
    *,
    legacy_repo: str,
    target_repo: str,
) -> MigrationDelta:
    """Classify every legacy endpoint/entity as *present-complete* or *absent*.

    Parameters
    ----------
    pairings:
        Output of :func:`same_app_correlator.correlate_contexts`.  One entry per
        legacy bounded context.
    legacy_digests:
        ``ModernizationDigest`` objects from the legacy repo.
    target_digests:
        ``ModernizationDigest`` objects from the target (rewrite) repo.
    legacy_repo:
        Identifier string for the legacy repo (stored verbatim in the output).
    target_repo:
        Identifier string for the target repo (stored verbatim in the output).

    Returns
    -------
    MigrationDelta
        One :class:`ContextDelta` per legacy context, containing one
        :class:`EndpointDelta` per legacy endpoint and one :class:`EntityDelta`
        per legacy entity.

    INVARIANT
    ---------
    Every legacy endpoint and entity appears **exactly once** in the returned
    delta — matched → ``"present-complete"``, unmatched → ``"absent"``.  Nothing
    is silently dropped.

    Note
    ----
    Stub detection (``is_stubbed``) and ``file:line`` evidence are wired at the
    pipeline layer (D4 / #1630) where handler bodies and discovery citations are
    available.  The digest-only core here classifies match-vs-absent only.
    """
    context_deltas: list[ContextDelta] = []

    for pairing in pairings:
        legacy_ifaces = _ifaces_for_context(legacy_digests, pairing.legacy_context)
        target_ifaces = (
            _ifaces_for_context(target_digests, pairing.target) if pairing.target else []
        )

        # Build a normalized-key → raw-target-iface lookup for matching.
        target_norm: dict[str, str] = {}
        for ti in target_ifaces:
            nk = normalize_iface(ti)
            # First occurrence wins (deterministic: keep stable).
            if nk not in target_norm:
                target_norm[nk] = ti

        # --- Endpoints (INVARIANT: every legacy iface accounted for) ---
        endpoint_deltas: list[EndpointDelta] = []
        for li in legacy_ifaces:
            matched_target = target_norm.get(normalize_iface(li))
            ep_disposition: Disposition
            ep_confidence: Confidence
            if matched_target is None:
                ep_disposition = "absent"
                ep_confidence = "high"
            else:
                ep_disposition = "present-complete"
                # Carry the correlator's match_confidence into the endpoint
                # (low match → medium confidence; high/medium match → high).
                ep_confidence = "high" if pairing.match_confidence == "high" else "medium"
            endpoint_deltas.append(
                EndpointDelta(
                    legacy=li,
                    legacy_evidence="",
                    target=matched_target,
                    target_evidence=None if matched_target is None else "",
                    disposition=ep_disposition,
                    confidence=ep_confidence,
                )
            )

        # --- Entities (INVARIANT: every legacy entity accounted for) ---
        legacy_entities = _entities_for_context(legacy_digests, pairing.legacy_context)
        target_entities_lower = (
            {e.lower() for e in _entities_for_context(target_digests, pairing.target)}
            if pairing.target
            else set()
        )

        entity_confidence: Confidence = "high" if pairing.match_confidence == "high" else "medium"
        entity_deltas: list[EntityDelta] = []
        for le in legacy_entities:
            ent_disposition: Disposition
            matched_entity: str | None
            if le.lower() in target_entities_lower:
                ent_disposition = "present-complete"
                matched_entity = le
            else:
                ent_disposition = "absent"
                matched_entity = None
            entity_deltas.append(
                EntityDelta(
                    legacy=le,
                    target=matched_entity,
                    disposition=ent_disposition,
                    confidence=entity_confidence,
                )
            )

        context_deltas.append(
            ContextDelta(
                context=pairing.legacy_context,
                match_confidence=pairing.match_confidence,
                endpoints=tuple(endpoint_deltas),
                entities=tuple(entity_deltas),
            )
        )

    return MigrationDelta(
        legacy_repo=legacy_repo,
        target_repo=target_repo,
        contexts=tuple(context_deltas),
    )


__all__ = ["assess_delta", "is_stubbed"]

"""Same-app twin-repo correlator (#1621 D2).

Pairs legacy↔target ``ModernizationDigest`` objects (one per bounded context)
using a **layered banded match**:

1. Exact route/entity match → ``high`` confidence.
2. Normalized rename match (strip verb/version/extension, ``.do``/``.action`` →
   bare, singular/plural fold) → ``high`` (≥ half legacy ifaces match) or
   ``medium`` (any normalized match).
3. Token-overlap (semantic tier) or context-name equality → ``medium``.
4. No match → ``target=None``, ``"low"``.

INVARIANT: every legacy context appears exactly once in the output.

QUAL-2: imports only stdlib / pydantic / ``_shared/modernization``; no LLM.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from primitives._shared.modernization.delta import ContextPairing
from primitives._shared.modernization.iface import canonical_path

if TYPE_CHECKING:
    from primitives._shared.modernization.models import ModernizationDigest

# ---------------------------------------------------------------------------
# Normalization helpers (Task 2)
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"/v\d+(?=/|$)")
_EXT_RE = re.compile(r"\.(do|action|jsp|php|aspx)$")
_SPLIT_RE = re.compile(r"[/:_\-]")
_PARTS_MAX = 2


def normalize_iface(name: str) -> str:
    """Return a normalized, verb-free, version-free interface key.

    *  ``"POST /checkout.do"`` and ``"POST /api/v2/checkout"`` both become
       ``"checkout"`` — so legacy Struts ``.do`` routes join their Spring Boot
       REST equivalents after normalization.
    *  ``"data:orders"`` and ``"data:order"`` both become ``"data:order"`` —
       a crude plural fold (strip trailing ``s``) applied to the body.
    *  HTTP interfaces: verb is dropped, ``/api`` prefix stripped, version
       segment stripped, ``.do``/``.action``/``.jsp``/``.php``/``.aspx``
       extension stripped, path-params collapsed via :func:`iface.canonical_path`,
       and each segment is singular-folded.

    The result is a plain lowercase string with no leading slash, suitable as a
    join key for :func:`correlate_contexts`.
    """
    if name.startswith("data:"):
        body = name[5:].strip().lower().rstrip("s")  # crude plural fold
        return f"data:{body}"

    # Split "METHOD /path" — drop the verb.
    parts = name.split(None, _PARTS_MAX - 1)
    path = parts[1] if len(parts) == _PARTS_MAX else parts[0]

    # Strip legacy extensions before canonicalising path params.
    path = _EXT_RE.sub("", path)
    # Strip version segments (/v1, /v2, …).
    path = _VERSION_RE.sub("", path)
    # Strip bare /api prefix (leaves /api/v2/… after version strip → /…).
    path = re.sub(r"^/api(?=/)", "", path)
    # Collapse path params to {}.
    path = canonical_path(path)

    # Singular-fold each path segment and lowercase.
    segs = [s.rstrip("s") for s in path.strip("/").split("/") if s]
    return "/".join(segs).lower()


def iface_tokens(name: str) -> frozenset[str]:
    """Split a normalized interface key into a set of non-empty tokens.

    Used for the semantic overlap tier in :func:`correlate_contexts`.
    """
    return frozenset(t for t in _SPLIT_RE.split(normalize_iface(name)) if t)


# ---------------------------------------------------------------------------
# Layered banded match (Task 3)
# ---------------------------------------------------------------------------


def _exposed_by_context(
    digests: list[ModernizationDigest],
) -> dict[str, list[str]]:
    """Build a ``{context_label: [interface_names]}`` index from a digest list.

    When a digest has no ``domains`` we fall back to ``code_root`` as the label
    so the context is never silently dropped.
    """
    out: dict[str, list[str]] = {}
    for d in digests:
        labels = d.domains if d.domains else [d.code_root]
        for label in labels:
            out.setdefault(label, []).extend(d.exposed_interfaces)
    return out


def _score(
    legacy_ifaces: list[str],
    target_ifaces: list[str],
) -> tuple[int, int, int]:
    """Return ``(exact, norm, sem)`` overlap counts.

    * ``exact`` — raw string matches (case-sensitive).
    * ``norm``  — normalized key matches.
    * ``sem``   — token-level overlap after normalization.
    """
    l_set = set(legacy_ifaces)
    t_set = set(target_ifaces)
    exact = len(l_set & t_set)

    l_norm = {normalize_iface(i) for i in legacy_ifaces}
    t_norm = {normalize_iface(i) for i in target_ifaces}
    norm = len(l_norm & t_norm)

    l_tok: set[str] = (
        set().union(*(iface_tokens(i) for i in legacy_ifaces)) if legacy_ifaces else set()
    )
    t_tok: set[str] = (
        set().union(*(iface_tokens(i) for i in target_ifaces)) if target_ifaces else set()
    )
    sem = len(l_tok & t_tok)

    return exact, norm, sem


def correlate_contexts(
    legacy_digests: list[ModernizationDigest],
    target_digests: list[ModernizationDigest],
) -> list[ContextPairing]:
    """Pair every legacy bounded context to the best-matching target context.

    Returns one :class:`~primitives._shared.modernization.delta.ContextPairing`
    per legacy context.  **INVARIANT: every legacy context appears exactly once
    in the returned list.**

    Banding rules (applied per legacy↔target candidate pair, best wins):

    * ``exact > 0`` → ``"high"``
    * ``norm > 0`` and ``norm ≥ ⌈len(legacy_ifaces)/2⌉`` → ``"high"``
    * ``norm > 0`` (any) → ``"medium"``
    * ``sem > 0`` OR context names are equal (case-insensitive) → ``"medium"``
    * No match → ``target=None``, ``"low"``

    Uses token-overlap (``iface_tokens``) for the semantic tier — this is the
    minimal implementation the plan specifies; ``feature_resolver`` signals are
    not wired here (they would be an optional layer above for a future task).
    """
    legacy_ctx = _exposed_by_context(legacy_digests)
    target_ctx = _exposed_by_context(target_digests)

    pairings: list[ContextPairing] = []
    for lctx, lifaces in sorted(legacy_ctx.items()):
        best: tuple[int, str, str] | None = None  # (rank, band, target_ctx)

        for tctx, tifaces in target_ctx.items():
            exact, norm, sem = _score(lifaces, tifaces)

            if exact > 0:
                band = "high"
            elif norm > 0:
                # "high" when at least half the legacy ifaces match normalized.
                threshold = max(1, (len(lifaces) + 1) // 2)
                band = "high" if norm >= threshold else "medium"
            elif sem > 0 or lctx.lower() == tctx.lower():
                band = "medium"
            else:
                continue  # no signal for this pair

            rank = {"high": 3, "medium": 2}[band]
            if best is None or rank > best[0]:
                best = (rank, band, tctx)

        if best is None:
            pairings.append(
                ContextPairing(
                    legacy_context=lctx,
                    target=None,
                    match_confidence="low",
                )
            )
        else:
            pairings.append(
                ContextPairing(
                    legacy_context=lctx,
                    target=best[2],
                    match_confidence=best[1],  # type: ignore[arg-type]
                )
            )

    return pairings


__all__ = ["correlate_contexts", "iface_tokens", "normalize_iface"]

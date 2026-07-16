"""Shared bounded-context resolver (Layer 1) — the one endpoint/FR→context join.

The app-mod chain distributes two kinds of functional-map facts across bounded
contexts:

* :func:`primitives.modernization_scope.shared.pipeline.distribute_frs` (#1356)
  assigns each FR to the spec whose domain label matches the FR's own ``domain``.
* :func:`primitives.functional_map.shared.slicing.slice_functional_map` (SP-A,
  #1381) assigns each endpoint (and, transitively, every functional-map fact tied
  to it) to the bounded context whose member set covers that endpoint.

Both are *the same deterministic join*: match a member key against a
``context → members`` map (case-insensitively, with a single-context fallback so a
key that matches nothing still lands in the one context that exists), else defer.
This module is the single source of truth for that join so the two callers can
never diverge (a divergent resolver is a latent bug — SP-A brief).

Pure and deterministic: no LLM, no clock, no filesystem.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence


def build_member_index(contexts: Mapping[str, Sequence[str]]) -> dict[str, str]:
    """Invert a ``context → members`` map into ``normalized-member → context``.

    Members are matched case-insensitively (lowercased + stripped), mirroring
    ``distribute_frs``'s ``str(fr["domain"]).lower()`` comparison. When two contexts
    claim the same member the *first* context (in the map's iteration order) wins;
    the map is expected to hold disjoint member sets (a system-map node belongs to
    one bounded context per its ``domains`` union), so a collision is a caller bug,
    not a silent-drop risk here.
    """
    index: dict[str, str] = {}
    for context, members in contexts.items():
        for member in members:
            key = str(member).strip().lower()
            if key and key not in index:
                index[key] = context
    return index


def resolve_context(
    keys: Iterable[str],
    member_index: Mapping[str, str],
    *,
    single_context: str | None,
) -> str | None:
    """Resolve the bounded context for a fact from its candidate *keys*.

    ``keys`` are every identifier the fact can be matched on (e.g. an endpoint's
    id + its ``http_iface`` string, or an FR's ``domain``). The first key that hits
    the ``member_index`` wins. Failing every key, the fact falls to
    ``single_context`` (the one-context app-mod case) — or ``None`` when there are
    multiple contexts and none matched, so the caller can *defer* it (never a silent
    drop).
    """
    for key in keys:
        normalized = str(key).strip().lower()
        if normalized and normalized in member_index:
            return member_index[normalized]
    return single_context


__all__ = ["build_member_index", "resolve_context"]

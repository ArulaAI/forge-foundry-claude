"""Canonical HTTP-interface strings for cross-layer correlation.

The app-modernization adapters (functional-map, api-contract-review, …) each turn a
different analysis artifact into a :class:`ModernizationDigest`. For
:func:`primitives._shared.modernization.correlate.build_system_map` to *join* a UI call to
the API endpoint it targets, both must render the **same** interface string — a
functional-map endpoint ``GET /owners/{ownerId}`` and an api-contract operation
``GET /owners/{id}`` must reduce to one key.

`correlate._normalize` only lowercases + strips a trailing ``/``; it does NOT reconcile
path-parameter spellings (``{ownerId}`` vs ``{id}`` vs ``:id`` vs ``*``) or method casing.
So the adapters call :func:`http_iface` to produce a canonical
``"<METHOD> <path>"`` string where every path parameter is collapsed to a single ``{}``
placeholder and the method is upper-cased. Two routes that differ only in parameter *names*
then normalize to the same join key.

This module is the single source of truth for that format — every adapter that emits an HTTP
interface MUST use it, so the correlation join is order-independent and name-independent.
"""

from __future__ import annotations

import re

#: One path segment that is a parameter, in any of the common spellings:
#: ``{ownerId}`` (Spring/OpenAPI), ``:id`` (Express/Rails), ``<int:pk>`` (Django),
#: ``*`` (wildcard), or a bare ``{}``. Matched per whole segment so a literal segment
#: containing a brace-free word is left alone.
_PARAM_SEGMENT_RE = re.compile(
    r"""
    ^(?:
        \{[^/]*\}        # {ownerId} / {} / {int:pk}
      | :[^/]+           # :id
      | <[^/]*>          # <int:pk>
      | \*+              # * / **
    )$
    """,
    re.VERBOSE,
)

_KNOWN_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)


def canonical_path(path: str) -> str:
    """Collapse every path parameter to ``{}`` and normalize slashes.

    ``/owners/{ownerId}/pets/{petId}/edit`` and ``/owners/:owner/pets/*`` both become
    ``/owners/{}/pets/{}`` (the second is shorter — only its own segments collapse). A
    leading slash is guaranteed; a trailing slash is stripped (matching
    ``correlate._normalize``).
    """
    raw = path.strip()
    # Drop a query string / fragment if one slipped in.
    raw = raw.split("?", 1)[0].split("#", 1)[0]
    segments = [seg for seg in raw.split("/") if seg != ""]
    out = ["{}" if _PARAM_SEGMENT_RE.match(seg) else seg for seg in segments]
    return "/" + "/".join(out)


def http_iface(method: str, path: str) -> str:
    """Return the canonical ``"<METHOD> <canonical-path>"`` interface string.

    ``method`` is upper-cased; an unknown/blank method falls back to ``ANY`` so the string
    is still well-formed and joinable. ``path`` is canonicalized via :func:`canonical_path`.

    Examples::

        http_iface("get", "/owners/{ownerId}")   -> "GET /owners/{}"
        http_iface("GET", "/owners/:id/")         -> "GET /owners/{}"
        http_iface("", "/vets.html")              -> "ANY /vets.html"
    """
    verb = method.strip().upper()
    if verb not in _KNOWN_METHODS:
        verb = "ANY"
    return f"{verb} {canonical_path(path)}"


__all__ = ["canonical_path", "http_iface"]

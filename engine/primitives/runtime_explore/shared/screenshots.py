"""Route-derived screenshot naming for runtime_explore (both phases).

A screen's screenshot is named by its **page/route** (not just its opaque screen id),
so the ``.foundry/context/runtime/screenshots/`` folder is visually browsable and a
consumer can look a screen up by route. The screen id is kept as a suffix, which
guarantees uniqueness (two screens on the same templatized route stay distinct) and a
stable join back to ``crawl-graph.json`` / ``runtime-journeys.json``.

    /owners/find   -> owners-find__SCR-001.png
    /owners/123    -> owners-{}__SCR-002.png   (concrete id segments templatized)
    /vets          -> vets__SCR-003.png
    /  (home)      -> home__SCR-004.png

This module also owns the path-templatization regex + helper (``_CONCRETE_SEGMENT_RE`` /
``templatize``) so the aggregator's runtime↔static route join and the screenshot slug
share ONE definition (DRY) — a runtime ``/owners/123`` and a static ``/owners/{ownerId}``
must collapse to the same ``/owners/{}`` shape in both places.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

# A concrete path segment that a templated static route would parameterize: a pure
# number, a UUID, or a long hex/opaque id. Collapsed to ``{}`` so a runtime URL like
# ``/owners/123`` joins the static template ``/owners/{ownerId}`` (both → ``/owners/{}``).
_CONCRETE_SEGMENT_RE = re.compile(r"^(?:\d+|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}|[0-9a-fA-F]{16,})$")

#: Max slug length (before the ``__<id>.png`` suffix) — keeps the filename well under
#: every filesystem's name limit even for deep paths. The id suffix still makes it unique.
_MAX_SLUG_LEN = 80

#: Everything that is NOT kept verbatim in a slug (we keep lowercase alnum + ``{}`` so a
#: templatized segment stays readable). Runs of these collapse to a single ``-``.
_NON_SLUG_RE = re.compile(r"[^a-z0-9{}]+")


def templatize(path: str) -> str:
    """Collapse concrete id-like path segments to ``{}`` (the static-template shape)."""
    segments = path.split("/")
    return "/".join("{}" if _CONCRETE_SEGMENT_RE.match(seg or "") else seg for seg in segments)


def _slugify_route(url: str) -> str:
    """Turn a URL into a filesystem-safe, route-readable slug (no scheme/host/query).

    Accepts either a full URL (``https://app/owners/find?x=1``) or a bare path
    (``/owners/find``) — ``urlsplit`` yields the path in both cases.
    """
    path = urlsplit(url).path or url  # bare relative paths land in .path; fall back to url
    slug = _NON_SLUG_RE.sub("-", templatize(path).lower()).strip("-")
    return slug[:_MAX_SLUG_LEN].strip("-") or "home"


def screenshot_name(screen_id: str, url: str, label: str | None = None) -> str:
    """Return the route-named PNG filename for a screen.

    Without a label:  ``<route-slug>__<id>.png``   (byte-identical to the pre-#1599 form)
    With a label:     ``<route-slug>__<label>__<id>.png``

    The label is slugified with the same ``_NON_SLUG_RE`` rule so it stays filename-safe.
    """
    route = _slugify_route(url)
    if label:
        safe_label = _NON_SLUG_RE.sub("-", label.lower()).strip("-")[:40]
        return f"{route}__{safe_label}__{screen_id}.png"
    return f"{route}__{screen_id}.png"


__all__ = ["screenshot_name", "templatize"]

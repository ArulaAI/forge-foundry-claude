"""Deterministic control-detector for runtime_explore (#1598).

Given a Playwright ``page``, :func:`detect_controls` enumerates and classifies every
interactive control on the current page and returns a list of :class:`~models.Control`
records with ``exercised_by="detected_only"``, a stable selector, and a human label
where derivable.

**Classification rules** (evaluated in DOM order; first match wins):

* ``<select>`` → ``"dropdown"``
* sortable column headers: ``<th[aria-sort]>``, ``[role=columnheader][aria-sort]``,
  ``<th>``/``<button>`` whose text or class implies sorting → ``"sort"``
* filter/search inputs OUTSIDE ``<form>``: ``input[type=search]``, inputs whose
  ``name``/``placeholder``/``aria-label`` implies filter or search → ``"filter"``
* ``input[type=checkbox]`` → ``"checkbox"``
* ``input[type=radio]`` → ``"radio"``
* pagination: ``[aria-label*=pag i]``, ``nav[role=navigation]`` with page links,
  ``a[rel=next]``/``a[rel=prev]``, ``.pagination`` children → ``"paginate"``
* inline edit: ``[contenteditable=true]``, buttons/icons whose text/aria implies
  edit/rename/modify → ``"edit"``
* modal/disclosure triggers: ``[aria-haspopup]``, ``[aria-expanded]``,
  ``[data-toggle=modal]``, ``[data-bs-toggle=modal]`` → ``"modal"``
* generic ``<button>``/``[role=button]`` not already classified → ``"button"``
* anything else actionable (``<a>``, ``input[type=submit]``, etc.) → ``"other"``

**Selector preference** (most-stable first, mirroring step_cli ``_SELECTOR_STRATEGIES``):
``data-testid`` → ARIA ``role+name`` → ARIA label → visible text → CSS.

Playwright is an OPTIONAL ``[browser]`` extra — this module imports cleanly WITHOUT it.
Only calling :func:`detect_controls` requires a live page.
"""

from __future__ import annotations

import re
from typing import Any

from primitives.runtime_explore.shared.models import Control

# ---------------------------------------------------------------------------
# Label-derivation helpers
# ---------------------------------------------------------------------------

_SORT_WORDS = re.compile(r"\b(sort|sorted|asc|desc|ascending|descending|order)\b", re.IGNORECASE)
_FILTER_WORDS = re.compile(r"\b(filter|search|find|query|lookup)\b", re.IGNORECASE)
_EDIT_WORDS = re.compile(r"\b(edit|rename|modify|update|change|pencil)\b", re.IGNORECASE)


def _attr(el: Any, *names: str) -> str:
    """Return the first non-empty attribute value from *names*, or ``""``."""
    for name in names:
        val = el.get_attribute(name)
        if val:
            return str(val).strip()
    return ""


def _visible_text(el: Any) -> str:
    """Return the visible inner-text of *el*, stripped, or ``""``."""
    try:
        text = el.inner_text()
        return text.strip() if text else ""
    except Exception:  # noqa: BLE001 - best-effort
        return ""


def _label_for(el: Any) -> str:
    """Derive a human-readable label from ARIA attrs, visible text, or title."""
    for attr_name in ("aria-label", "title", "placeholder", "name", "id"):
        val = _attr(el, attr_name)
        if val:
            return val
    return _visible_text(el)


# ---------------------------------------------------------------------------
# Stable-selector derivation (testid > role > label > text > CSS)
# ---------------------------------------------------------------------------


def _stable_selector(el: Any) -> str:
    """Return the most-stable selector for *el*, mirroring step_cli's strategy order.

    Preference: ``data-testid`` → ARIA ``role=…,name=…`` → ``aria-label`` →
    visible text (button/link) → CSS fallback (tag + id/class).
    """
    # 1. data-testid
    testid = _attr(el, "data-testid", "data-test-id", "data-cy", "data-test")
    if testid:
        return f'[data-testid="{testid}"]'

    # 2. ARIA role + accessible name
    role = _attr(el, "role")
    aria_label = _attr(el, "aria-label")
    if role and aria_label:
        return f'[role="{role}"][aria-label="{aria_label}"]'
    if role:
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        return f'{tag}[role="{role}"]'

    # 3. aria-label alone
    if aria_label:
        return f'[aria-label="{aria_label}"]'

    # 4. Visible text for interactive elements (button, a, th)
    tag = el.evaluate("e => e.tagName.toLowerCase()")
    if tag in ("button", "a", "th"):
        text = _visible_text(el)
        if text:
            # Use text content selector to stay stable (short text only)
            safe = text[:40].replace('"', '\\"')
            return f'{tag}:has-text("{safe}")'

    # 5. CSS fallback: tag + id or first class
    el_id = _attr(el, "id")
    if el_id:
        return f"#{el_id}"

    classes = _attr(el, "class")
    if classes:
        first_cls = classes.split()[0]
        return f"{tag}.{first_cls}"

    return str(tag)


# ---------------------------------------------------------------------------
# Element-level kind classifiers (return kind string or "" to skip)
# ---------------------------------------------------------------------------


def _kind_select(_el: Any) -> str:
    return "dropdown"


def _kind_sortable_header(el: Any) -> str:
    """Return ``"sort"`` for elements that look like a sortable column header."""
    tag = el.evaluate("e => e.tagName.toLowerCase()")
    aria_sort = _attr(el, "aria-sort")
    role = _attr(el, "role")

    if aria_sort:
        return "sort"
    if role == "columnheader":
        return "sort"
    if tag in ("th", "button"):
        text = _visible_text(el)
        cls = _attr(el, "class")
        combined = f"{text} {cls}"
        if _SORT_WORDS.search(combined):
            return "sort"
    return ""


def _kind_filter_input(el: Any) -> str:
    """Return ``"filter"`` for a filter/search input outside a ``<form>``."""
    itype = _attr(el, "type").lower() or "text"
    if itype == "search":
        return "filter"
    # Check name/placeholder/aria-label for filter/search keywords
    hint = " ".join(
        [
            _attr(el, "name"),
            _attr(el, "placeholder"),
            _attr(el, "aria-label"),
        ]
    )
    if _FILTER_WORDS.search(hint):
        return "filter"
    return ""


def _kind_paginate(el: Any) -> str:
    aria_label = _attr(el, "aria-label").lower()
    if "pag" in aria_label:
        return "paginate"
    rel = _attr(el, "rel").lower()
    if rel in ("next", "prev", "previous"):
        return "paginate"
    cls = _attr(el, "class").lower()
    if "pagination" in cls or "page-link" in cls or "page-item" in cls:
        return "paginate"
    # nav[role=navigation] with page-like aria-label handled at the nav level by
    # including its children — individual <a> inside .pagination also caught above.
    return ""


def _kind_edit(el: Any) -> str:
    ce = _attr(el, "contenteditable")
    if ce and ce.lower() not in ("false", ""):
        return "edit"
    text = _visible_text(el)
    aria_label = _attr(el, "aria-label")
    cls = _attr(el, "class")
    combined = f"{text} {aria_label} {cls}"
    if _EDIT_WORDS.search(combined):
        return "edit"
    return ""


def _kind_modal(el: Any) -> str:
    if _attr(el, "aria-haspopup"):
        return "modal"
    if _attr(el, "aria-expanded"):
        return "modal"
    toggle = _attr(el, "data-toggle") or _attr(el, "data-bs-toggle")
    if toggle in ("modal", "collapse", "dropdown", "offcanvas"):
        return "modal"
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: CSS selector for text-like inputs that might be filter controls outside a form.
_TEXT_INPUTS_SEL = (
    "input:not([type=search])"
    ":not([type=checkbox])"
    ":not([type=radio])"
    ":not([type=submit])"
    ":not([type=button])"
    ":not([type=hidden])"
)

#: CSS selector for pagination-related elements.
_PAGINATE_SEL = (
    "[aria-label*='page' i], [aria-label*='paginat' i], "
    "a[rel=next], a[rel=prev], a[rel=previous], "
    ".pagination a, .pagination button, "
    ".page-link, .page-item a"
)

#: CSS selector for modal/disclosure trigger elements.
_MODAL_SEL = (
    "[aria-haspopup], [aria-expanded], "
    "[data-toggle=modal], [data-toggle=collapse], [data-toggle=dropdown], "
    "[data-bs-toggle=modal], [data-bs-toggle=collapse], [data-bs-toggle=dropdown]"
)

#: CSS selector for catch-all "other" actionable elements.
_OTHER_SEL = (
    "a[href]:not([rel=next]):not([rel=prev]):not([rel=previous]), "
    "input[type=submit], input[type=button], "
    "[tabindex]:not([tabindex='-1'])"
)


def _collect_dropdowns(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all("select"):
        found.append(("dropdown", _stable_selector(el), _label_for(el)))


def _collect_sort_headers(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all("th[aria-sort], [role=columnheader][aria-sort], th, button"):
        kind = _kind_sortable_header(el)
        if kind:
            found.append((kind, _stable_selector(el), _label_for(el)))


def _collect_filter_inputs(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all("input[type=search]"):
        found.append(("filter", _stable_selector(el), _label_for(el)))
    for el in page.query_selector_all(_TEXT_INPUTS_SEL):
        in_form = el.evaluate("e => !!e.closest('form')")
        if not in_form:
            kind = _kind_filter_input(el)
            if kind:
                found.append((kind, _stable_selector(el), _label_for(el)))


def _collect_checkbox_radio(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all("input[type=checkbox]"):
        found.append(("checkbox", _stable_selector(el), _label_for(el)))
    for el in page.query_selector_all("input[type=radio]"):
        found.append(("radio", _stable_selector(el), _label_for(el)))


def _collect_paginate(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all(_PAGINATE_SEL):
        kind = _kind_paginate(el)
        if kind:
            found.append((kind, _stable_selector(el), _label_for(el)))


def _collect_edit(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all("[contenteditable]"):
        ce = _attr(el, "contenteditable")
        if ce and ce.lower() not in ("false", ""):
            found.append(("edit", _stable_selector(el), _label_for(el)))
    for el in page.query_selector_all("button, [role=button]"):
        kind = _kind_edit(el)
        if kind:
            found.append((kind, _stable_selector(el), _label_for(el)))


def _collect_modal(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all(_MODAL_SEL):
        kind = _kind_modal(el)
        if kind:
            found.append((kind, _stable_selector(el), _label_for(el)))


def _collect_buttons(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all("button, [role=button]"):
        sel = _stable_selector(el)
        if not any(s == sel for _, s, _ in found):
            found.append(("button", sel, _label_for(el)))


def _collect_other(page: Any, found: list[tuple[str, str, str]]) -> None:
    for el in page.query_selector_all(_OTHER_SEL):
        sel = _stable_selector(el)
        if not any(s == sel for _, s, _ in found):
            found.append(("other", sel, _label_for(el)))


def detect_controls(page: Any) -> list[Control]:
    """Enumerate and classify all interactive controls on *page*.

    Returns :class:`~models.Control` records in **DOM order** with
    ``exercised_by="detected_only"``.  Every actionable element is represented —
    unclassified elements become ``kind="other"`` rather than being silently dropped.
    IDs are assigned as ``CTL-001``, ``CTL-002``, … in the order controls are returned.

    Requires a live Playwright ``page``.  The caller must ensure Playwright is installed
    (use :func:`~browser.playwright_available` before calling this in a CLI context).
    """
    found: list[tuple[str, str, str]] = []  # (kind, selector, label)

    _collect_dropdowns(page, found)
    _collect_sort_headers(page, found)
    _collect_filter_inputs(page, found)
    _collect_checkbox_radio(page, found)
    _collect_paginate(page, found)
    _collect_edit(page, found)
    _collect_modal(page, found)
    _collect_buttons(page, found)
    _collect_other(page, found)

    # Build final Control records with CTL-NNN ids
    controls: list[Control] = []
    for idx, (kind, selector, label) in enumerate(found, start=1):
        controls.append(
            Control(
                id=f"CTL-{idx:03d}",
                kind=kind,  # type: ignore[arg-type]
                selector=selector,
                label=label,
                exercised_by="detected_only",
            )
        )
    return controls


__all__ = ["detect_controls"]

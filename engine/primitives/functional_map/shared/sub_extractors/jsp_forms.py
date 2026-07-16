from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Spring form tags
_FORM_INPUT_RE = re.compile(r'<form:input\s[^>]*path="([^"]+)"', re.IGNORECASE)
# <form:select> / <select> harvesting moved to _harvest_selects (Track D, #1426)
# so the real <option> literals travel with the field.
_FORM_CHECKBOX_RE = re.compile(r'<form:checkbox\s[^>]*path="([^"]+)"', re.IGNORECASE)
_FORM_TEXTAREA_RE = re.compile(r'<form:textarea\s[^>]*path="([^"]+)"', re.IGNORECASE)
_FORM_ERRORS_RE = re.compile(r'<form:errors\s[^>]*path="([^"]+)"', re.IGNORECASE)
_FORM_ACTION_RE = re.compile(r'<form\b[^>]*\baction="([^"]+)"', re.IGNORECASE)

# Plain HTML inputs
_HTML_INPUT_RE = re.compile(r'<input\s[^>]*type="([^"]+)"[^>]*name="([^"]+)"', re.IGNORECASE)
_HTML_INPUT_NAME_FIRST_RE = re.compile(
    r'<input\s[^>]*name="([^"]+)"[^>]*type="([^"]+)"', re.IGNORECASE
)

# Custom taglib field tags (#1350) — a GENERALIZED detector for any app's custom
# form-input tag library (not just Spring PetClinic's ``<petclinic:inputField>`` /
# ``<petclinic:selectField>``). A custom input tag is a NAMESPACED tag
# (``<prefix:localName …>``) whose *local name* looks input-ish — it contains
# ``input``, ``field``, ``select``, ``text``, ``checkbox``, ``radio``, or ``combo``
# (case-insensitive) — AND carries a ``name="…"`` or ``path="…"`` value attribute.
# Requiring BOTH an input-ish local name and a value attribute keeps non-input custom
# tags (``<sec:authorize>``, ``<c:set var=…>``, ``<display:column>``) from matching.
# The ``form:`` prefix is excluded (handled by the Spring ``form:*`` patterns above);
# the field's ``type`` is inferred from the local name (``select``/``combo`` → select,
# ``checkbox`` → checkbox, ``radio`` → radio, else text).
_CUSTOM_FIELD_RE = re.compile(
    r"<(?!form:)[A-Za-z][\w.-]*:"  # a namespaced (non-form:) tag
    r"(\w*(?:input|field|select|text|checkbox|radio|combo)\w*)"  # input-ish local name
    r'\s[^>]*\b(?:name|path)="([^"]+)"',  # a value attribute
    re.IGNORECASE,
)


def _custom_field_type(local_name: str) -> str:
    """Infer the field_type from a custom tag's local name."""
    low = local_name.lower()
    if "select" in low or "combo" in low:
        return "select"
    if "checkbox" in low:
        return "checkbox"
    if "radio" in low:
        return "radio"
    return "text"


def _harvest_custom_fields(
    content: str,
    *,
    seen: set[str],
    validated_fields: set[str],
    conditional_fields: dict[str, str],
    form_action: str,
    start_order: int,
) -> list[dict[str, object]]:
    """Harvest custom-taglib input fields (#1350) from ``content``.

    Any namespaced input-ish tag (``<prefix:inputField name="x"/>`` and friends,
    per :data:`_CUSTOM_FIELD_RE`) with a value attribute, deduped against ``seen``.
    Returns the new field dicts; ``display_order`` continues from ``start_order``.
    """
    fields: list[dict[str, object]] = []
    order = start_order
    for m in _CUSTOM_FIELD_RE.finditer(content):
        local_name, name = m.group(1), m.group(2)
        if name in seen:
            continue
        seen.add(name)
        order += 1
        fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": _custom_field_type(local_name),
                "required": name in validated_fields,
                "constraints": (),
                "source": "jsp",
                "display_order": order,
                "conditional_on": conditional_fields.get(name, ""),
                "conditional_value": "",
                "form_action": form_action,
            }
        )
    return fields


# Real select/dropdown option lists (Track D, #1426). A ``<select …>`` /
# ``<form:select …>`` block, its inner ``<option …>`` / ``<form:option …>``
# literals harvested VERBATIM so the spec inlines the real enum values. The
# submitted literal (the ``value="…"`` attribute) is preferred over the visible
# label; a plain ``<option>Active</option>`` falls back to its text.
_SELECT_BLOCK_RE = re.compile(
    r"<(form:select|select)\b([^>]*)>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
_SELECT_NAME_RE = re.compile(r'\b(?:name|path)="([^"]+)"', re.IGNORECASE)
_OPTION_RE = re.compile(
    r"<(?:form:)?option\b([^>]*?)(?:/>|>(.*?)</(?:form:)?option>)",
    re.IGNORECASE | re.DOTALL,
)
_OPTION_VALUE_RE = re.compile(r'\bvalue="([^"]*)"', re.IGNORECASE)


# A self-closing / data-bound select with no literal options:
#   <form:select path="ownerId" items="${owners}"/>  or  <select name="x"></select>
_SELF_CLOSING_SELECT_RE = re.compile(
    r"<(?:form:)?select\b([^>]*?)/>",
    re.IGNORECASE,
)


def _harvest_selects(
    content: str, *, form_action: str, start_order: int
) -> list[dict[str, object]]:
    """Harvest every ``<select>``/``<form:select>`` field with its real options.

    Covers Spring ``<form:select>`` (with ``<form:option>`` children), plain HTML
    ``<select>`` (with ``<option>`` children), and self-closing / data-bound
    selects (options come from the model → empty ``options``, no fabrication).
    Deterministic; option literals harvested VERBATIM. ``display_order`` continues
    from ``start_order``.
    """
    fields: list[dict[str, object]] = []
    seen: set[str] = set()
    order = start_order
    for block in _SELECT_BLOCK_RE.finditer(content):
        name, options = _harvest_select_options(block.group(2), block.group(3))
        if not name or name in seen:
            continue
        seen.add(name)
        order += 1
        fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": "select",
                "required": False,
                "constraints": (),
                "options": options,
                "source": "jsp",
                "display_order": order,
                "form_action": form_action,
            }
        )
    for m in _SELF_CLOSING_SELECT_RE.finditer(content):
        name_m = _SELECT_NAME_RE.search(m.group(1))
        name = name_m.group(1) if name_m else ""
        if not name or name in seen:
            continue
        seen.add(name)
        order += 1
        fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": "select",
                "required": False,
                "constraints": (),
                "options": (),
                "source": "jsp",
                "display_order": order,
                "form_action": form_action,
            }
        )
    return fields


def _harvest_select_options(attrs: str, inner: str) -> tuple[str, tuple[str, ...]]:
    """Return ``(field_name, options)`` for one ``<select>``/``<form:select>`` block.

    ``options`` is the literal option list harvested VERBATIM — the ``value``
    attribute when present, else the visible option text. A data-bound select
    with no literal ``<option>`` tags yields ``()`` (no fabrication).
    """
    name_m = _SELECT_NAME_RE.search(attrs)
    name = name_m.group(1) if name_m else ""
    options: list[str] = []
    for opt in _OPTION_RE.finditer(inner):
        opt_attrs, text = opt.group(1) or "", opt.group(2) or ""
        val_m = _OPTION_VALUE_RE.search(opt_attrs)
        literal = val_m.group(1) if val_m else re.sub(r"<[^>]+>", "", text).strip()
        if literal:
            options.append(literal)
    return name, tuple(options)


# i18n message keys
_MSG_CODE_RE = re.compile(r'<spring:message\s[^>]*code="([^"]+)"', re.IGNORECASE)

# JSTL conditional: <c:if test="${X}"> ... </c:if>
_COND_OPEN_RE = re.compile(r'<c:if\s+test="\$\{([^}]+)\}"', re.IGNORECASE)
_COND_CLOSE_RE = re.compile(r"</c:if>", re.IGNORECASE)


def parse(file_path: Path, content: str) -> dict[str, Any]:  # noqa: PLR0912
    """Returns {} if file is not .jsp/.jspx or has no form content."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".jsp", ".jspx"):
        return {}
    # A JSP is in scope if it has a form, a Spring form: tag, OR any custom
    # namespaced input-ish field tag (#1350). The custom-field regex is the
    # authoritative check; the cheap substring pre-filter avoids running it on
    # files with no field-tag signal at all.
    has_custom_field = _CUSTOM_FIELD_RE.search(content) is not None
    if "<form" not in content and "form:" not in content and not has_custom_field:
        return {}

    form_fields: list[dict[str, Any]] = []
    i18n_refs: list[str] = []
    order = 0

    action_m = _FORM_ACTION_RE.search(content)
    form_action = action_m.group(1) if action_m else ""

    for m in _MSG_CODE_RE.finditer(content):
        i18n_refs.append(m.group(1))

    # Track which fields are inside a <c:if> conditional
    conditional_fields: dict[str, str] = {}
    lines = content.split("\n")
    current_condition: str | None = None
    depth = 0
    for line in lines:
        open_m = _COND_OPEN_RE.search(line)
        close_m = _COND_CLOSE_RE.search(line)
        if open_m:
            depth += 1
            current_condition = open_m.group(1)
        if current_condition:
            for fm in _FORM_INPUT_RE.finditer(line):
                conditional_fields[fm.group(1)] = current_condition
        if close_m:
            depth -= 1
            if depth <= 0:
                current_condition = None
                depth = 0

    # Fields with server-side validation (from form:errors)
    validated_fields: set[str] = set()
    for m in _FORM_ERRORS_RE.finditer(content):
        validated_fields.add(m.group(1))

    for m in _FORM_INPUT_RE.finditer(content):
        name = m.group(1)
        cond = conditional_fields.get(name, "")
        conditional_on = ""
        conditional_value = ""
        if cond:
            eq_m = re.search(r"\w+\.(\w+)\s*==\s*'([^']+)'", cond)
            if eq_m:
                conditional_on = eq_m.group(1)
                conditional_value = eq_m.group(2)
            else:
                conditional_on = cond

        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": "text",
                "required": name in validated_fields,
                "constraints": (),
                "source": "jsp",
                "display_order": order,
                "conditional_on": conditional_on,
                "conditional_value": conditional_value,
                "form_action": form_action,
            }
        )

    # Selects/dropdowns with their real option lists (Track D, #1426) — covers
    # <form:select>/<select> blocks (option literals harvested VERBATIM) and
    # self-closing / data-bound selects (empty options; no fabrication).
    select_fields = _harvest_selects(content, form_action=form_action, start_order=order)
    form_fields.extend(select_fields)
    order += len(select_fields)

    for m in _FORM_CHECKBOX_RE.finditer(content):
        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": m.group(1),
                "field_type": "checkbox",
                "required": False,
                "constraints": (),
                "source": "jsp",
                "display_order": order,
                "form_action": form_action,
            }
        )

    for m in _FORM_TEXTAREA_RE.finditer(content):
        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": m.group(1),
                "field_type": "textarea",
                "required": False,
                "constraints": (),
                "source": "jsp",
                "display_order": order,
                "form_action": form_action,
            }
        )

    # Custom taglib fields (#1350): any namespaced input-ish tag with a name/path
    # attribute, e.g. <petclinic:inputField name="firstName"/> or a different app's
    # <ui:textField path="x"/>. An app that renders all inputs through a custom tag
    # library otherwise harvests zero fields. Dedup against names captured above.
    custom_seen: set[str] = {f["name"] for f in form_fields}
    custom_fields = _harvest_custom_fields(
        content,
        seen=custom_seen,
        validated_fields=validated_fields,
        conditional_fields=conditional_fields,
        form_action=form_action,
        start_order=order,
    )
    form_fields.extend(custom_fields)
    order += len(custom_fields)

    # Plain HTML inputs (including hidden), skip already-seen names
    seen_names: set[str] = {f["name"] for f in form_fields}
    for m in _HTML_INPUT_RE.finditer(content):
        itype, name = m.group(1).lower(), m.group(2)
        if name in seen_names:
            continue
        seen_names.add(name)
        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": "hidden" if itype == "hidden" else itype,
                "required": False,
                "constraints": (),
                "source": "hidden" if itype == "hidden" else "jsp",
                "display_order": order,
                "form_action": form_action,
            }
        )
    for m in _HTML_INPUT_NAME_FIRST_RE.finditer(content):
        name, itype = m.group(1), m.group(2).lower()
        if name in seen_names:
            continue
        seen_names.add(name)
        order += 1
        form_fields.append(
            {
                "endpoint_id": "",
                "name": name,
                "field_type": "hidden" if itype == "hidden" else itype,
                "required": False,
                "constraints": (),
                "source": "hidden" if itype == "hidden" else "jsp",
                "display_order": order,
                "form_action": form_action,
            }
        )

    result: dict[str, Any] = {}
    if form_fields:
        result["form_fields"] = form_fields
    if i18n_refs:
        result["i18n_refs"] = i18n_refs
    return result

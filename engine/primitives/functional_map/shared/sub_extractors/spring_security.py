from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from defusedxml import ElementTree as ET

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

# <sec:authorize access="...">
_SEC_AUTHORIZE_RE = re.compile(r'<sec:authorize\s[^>]*\baccess\s*=\s*"([^"]*)"', re.DOTALL)
# <*:ifRole ...> or <*:authorize ...>
_CUSTOM_TAGLIB_RE = re.compile(r"<[a-zA-Z][\w]*:(ifRole|authorize)\b[^>]*>", re.DOTALL)

# Method-level security annotations on Java controllers
_PRE_AUTHORIZE_RE = re.compile(r'@PreAuthorize\s*\(\s*"([^"]+)"\s*\)')
_SECURED_RE = re.compile(r'@Secured\s*\(\s*(\{[^}]+\}|"[^"]+")\s*\)')
_ROLES_ALLOWED_RE = re.compile(r'@RolesAllowed\s*\(\s*(\{[^}]+\}|"[^"]+")\s*\)')
# hasRole / hasAnyRole role extraction
_HAS_ROLE_RE = re.compile(r"""hasAnyRole\s*\(([^)]+)\)|hasRole\s*\(\s*['"]([^'"]+)['"]\s*\)""")
# @GetMapping/@PostMapping etc. path extraction (next few lines after security annotation)
_MAPPING_ANNOTATION_RE = re.compile(
    r'@(Get|Post|Put|Patch|Delete|Request)Mapping\s*(?:\(\s*(?:value\s*=\s*)?["\']([^"\']*)["\'])?',
    re.IGNORECASE,
)
# Class-level @RequestMapping prefix
_CLASS_REQUEST_MAPPING_RE = re.compile(
    r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
    re.DOTALL,
)
_CONTROLLER_SIGNALS = ("@Controller", "@RestController")
_METHOD_SECURITY_SIGNALS = ("@PreAuthorize", "@Secured", "@RolesAllowed")
_HTTP_VERB_MAP = {
    "GET": "GET",
    "POST": "POST",
    "PUT": "PUT",
    "PATCH": "PATCH",
    "DELETE": "DELETE",
    "REQUEST": "GET",
}


def _strip_role(name: str) -> str:
    return name[5:] if name.startswith("ROLE_") else name


def _tag(el: Element) -> str:
    tag = el.tag
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _extract_roles_from_expr(expr: str) -> list[str]:
    """Extract roles from hasRole/hasAnyRole SpEL expressions; empty list if none."""
    roles: list[str] = []
    for m in _HAS_ROLE_RE.finditer(expr):
        if m.group(1):  # hasAnyRole(...)
            for part in re.findall(r"""['"]([^'"]+)['"]""", m.group(1)):
                roles.append(_strip_role(part.strip()))
        elif m.group(2):  # hasRole(...)
            roles.append(_strip_role(m.group(2).strip()))
    return roles


def _extract_roles_from_value(raw: str) -> list[str]:
    """Extract roles from @Secured/@RolesAllowed value strings or arrays."""
    return [_strip_role(r.strip()) for r in re.findall(r"""['"]([^'"]+)['"]""", raw)]


def _find_mapping_after(lines: list[str], start: int) -> tuple[str, str]:
    """Scan up to 5 lines after start for a mapping annotation; return (path_suffix, method)."""
    for j in range(start, min(start + 5, len(lines))):
        m = _MAPPING_ANNOTATION_RE.search(lines[j])
        if m:
            verb = m.group(1).upper()
            http_method = _HTTP_VERB_MAP.get(verb, "GET")
            path_suffix = m.group(2) or ""
            return path_suffix, http_method
    return "", ""


def _parse_java_method_security(
    file_path: Path, content: str, lines: list[str]
) -> list[dict[str, object]]:
    """Extract guards from @PreAuthorize/@Secured/@RolesAllowed on controller methods."""
    if not any(sig in content for sig in _CONTROLLER_SIGNALS):
        return []
    if not any(sig in content for sig in _METHOD_SECURITY_SIGNALS):
        return []

    class_prefix = ""
    cm = _CLASS_REQUEST_MAPPING_RE.search(content)
    if cm:
        class_prefix = cm.group(1).rstrip("/")

    guards: list[dict[str, object]] = []

    for i, line in enumerate(lines):
        roles: list[str] = []
        kind: str = "role_required"
        matched = False

        m = _PRE_AUTHORIZE_RE.search(line)
        if m:
            expr = m.group(1)
            roles = _extract_roles_from_expr(expr)
            if not roles:
                kind = "auth_required" if "isAuthenticated()" in expr else "policy"
            matched = True

        if not matched:
            m = _SECURED_RE.search(line)
            if m:
                roles = _extract_roles_from_value(m.group(1))
                matched = True

        if not matched:
            m = _ROLES_ALLOWED_RE.search(line)
            if m:
                roles = _extract_roles_from_value(m.group(1))
                matched = True

        if not matched:
            continue

        path_suffix, http_method = _find_mapping_after(lines, i)
        full_path = (class_prefix + path_suffix) if path_suffix else class_prefix

        line_num = content[: sum(len(ln) + 1 for ln in lines[:i])].count("\n") + 1
        guards.append(
            {
                "endpoint_id": "",
                "path": full_path,
                "method": http_method,
                "kind": kind,
                "roles": roles,
                "file": str(file_path),
                "line": line_num,
            }
        )

    return guards


def parse(file_path: Path, content: str) -> dict[str, list[object]]:  # noqa: PLR0912
    """Returns {} if file has no Spring Security signals."""
    name = Path(file_path).name
    suffix = Path(file_path).suffix.lower()

    is_web_xml = name == "web.xml"
    has_sec_authorize = "<sec:authorize" in content
    has_security_constraint = "<security-constraint" in content
    has_custom_taglib = bool(_CUSTOM_TAGLIB_RE.search(content))
    has_method_security = suffix == ".java" and any(
        sig in content for sig in _METHOD_SECURITY_SIGNALS
    )

    if not (
        is_web_xml
        or has_sec_authorize
        or has_security_constraint
        or has_custom_taglib
        or has_method_security
    ):
        return {}

    guards: list[dict[str, object]] = []

    # --- web.xml <security-constraint> parsing ---
    if (is_web_xml or has_security_constraint) and suffix == ".xml":
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            root = None

        if root is not None:
            for constraint in root.iter():
                if _tag(constraint) != "security-constraint":
                    continue
                url_patterns: list[str] = []
                roles: list[str] = []
                for child in constraint:
                    ctag = _tag(child)
                    if ctag == "web-resource-collection":
                        for sub in child:
                            if _tag(sub) == "url-pattern" and sub.text:
                                url_patterns.append(sub.text.strip())
                    elif ctag == "auth-constraint":
                        for sub in child:
                            if _tag(sub) == "role-name" and sub.text:
                                roles.append(_strip_role(sub.text.strip()))
                for pattern in url_patterns:
                    sc_idx = content.find("<security-constraint")
                    search_from = sc_idx if sc_idx != -1 else 0
                    idx = content.find(f"<url-pattern>{pattern}</url-pattern>", search_from)
                    if idx == -1:
                        idx = sc_idx if sc_idx != -1 else 0
                    line = content[:idx].count("\n") + 1 if idx != -1 else 1
                    guards.append(
                        {
                            "endpoint_id": "",
                            "url_pattern": pattern,
                            "kind": "role_required",
                            "roles": roles,
                            "file": str(file_path),
                            "line": line,
                        }
                    )

    # --- JSP/JSPX/Java: <sec:authorize access="..."> ---
    for m in _SEC_AUTHORIZE_RE.finditer(content):
        line = content[: m.start()].count("\n") + 1
        guards.append(
            {
                "endpoint_id": "",
                "url_pattern": "",
                "kind": "policy",
                "roles": [],
                "file": str(file_path),
                "line": line,
            }
        )

    # --- Custom taglibs <*:ifRole> / <*:authorize> (non-sec: prefix) ---
    for m in _CUSTOM_TAGLIB_RE.finditer(content):
        # skip sec: prefix — already handled above
        tag_text = m.group(0)
        if tag_text.startswith("<sec:"):
            continue
        line = content[: m.start()].count("\n") + 1
        guards.append(
            {
                "endpoint_id": "",
                "url_pattern": "",
                "kind": "policy",
                "roles": [],
                "file": str(file_path),
                "line": line,
            }
        )

    # --- Java method-level security annotations ---
    if has_method_security:
        lines = content.split("\n")
        guards.extend(_parse_java_method_security(file_path, content, lines))

    if not guards:
        return {}
    return {"guards": guards}  # type: ignore[dict-item]

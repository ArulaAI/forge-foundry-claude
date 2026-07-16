from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

from defusedxml import ElementTree as ET

_SERVLET_SIGNAL = ("@WebServlet", "HttpServlet", "extends HttpServlet")
_WEB_XML_SIGNAL = ("<servlet-mapping", "<web-app")

# @WebServlet url patterns
_URL_PATTERN_RE = re.compile(
    r"@WebServlet\s*\(\s*(?:urlPatterns\s*=\s*)?\{([^}]+)\}"
    r'|@WebServlet\s*\(\s*"([^"]+)"\s*\)'
    r'|@WebServlet\s*\(\s*urlPatterns\s*=\s*"([^"]+)"\s*\)',
    re.DOTALL,
)
# doGet / doPost / doPut / doDelete
_DO_METHOD_RE = re.compile(
    r"protected\s+void\s+(doGet|doPost|doPut|doDelete|doPatch)\s*\(",
)
# req.getParameter("name")
_GET_PARAM_RE = re.compile(r'req\.getParameter\s*\(\s*"([^"]+)"\s*\)')
# session read/write — handles both session.X and req.getSession().X
_SESSION_SET_RE = re.compile(r'(?:req\.getSession\(\)|session)\.setAttribute\s*\(\s*"([^"]+)"')
_SESSION_GET_RE = re.compile(r'(?:req\.getSession\(\)|session)\.getAttribute\s*\(\s*"([^"]+)"')
# sendRedirect
_REDIRECT_RE = re.compile(r'(?:resp\.sendRedirect|response\.sendRedirect)\s*\(\s*"([^"]+)"')
# RequestDispatcher forward
_FORWARD_RE = re.compile(r'getRequestDispatcher\s*\(\s*"([^"]+)"\s*\)\.forward')

_HTTP_VERB = {
    "doGet": "GET",
    "doPost": "POST",
    "doPut": "PUT",
    "doDelete": "DELETE",
    "doPatch": "PATCH",
}


def _tag(el: Element) -> str:
    tag = el.tag
    return tag.split("}", 1)[1] if tag.startswith("{") else tag


def _parse_web_xml(file_path: Path, content: str) -> dict[str, Any]:
    """Parse web.xml <servlet>/<servlet-mapping> → endpoints."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return {}

    # name → class
    name_to_class: dict[str, str] = {}
    for el in root.iter():
        if _tag(el) != "servlet":
            continue
        sname = sclass = ""
        for child in el:
            t = _tag(child)
            if t == "servlet-name" and child.text:
                sname = child.text.strip()
            elif t == "servlet-class" and child.text:
                sclass = child.text.strip()
        if sname and sclass:
            name_to_class[sname] = sclass

    # name → all url-patterns (a servlet may have several mappings / patterns)
    name_to_patterns: dict[str, list[str]] = {}
    for el in root.iter():
        if _tag(el) != "servlet-mapping":
            continue
        sname = ""
        patterns: list[str] = []
        for child in el:
            t = _tag(child)
            if t == "servlet-name" and child.text:
                sname = child.text.strip()
            elif t == "url-pattern" and child.text:
                patterns.append(child.text.strip())
        if sname and patterns:
            name_to_patterns.setdefault(sname, []).extend(patterns)

    # session-timeout hint
    session_hint: str = ""
    for el in root.iter():
        if _tag(el) == "session-timeout" and el.text:
            session_hint = f"session-timeout:{el.text.strip()}min"
            break

    endpoints: list[dict[str, Any]] = []
    business_contexts: list[dict[str, Any]] = []
    for sname, patterns in name_to_patterns.items():
        handler = name_to_class.get(sname, sname)
        for pattern in patterns:
            ep = {
                "path": pattern,
                "method": "GET",
                "handler": handler,
                "file": str(file_path),
                "line": 0,
            }
            endpoints.append(ep)
            hints = (session_hint,) if session_hint else ()
            business_contexts.append(
                {
                    "path": pattern,
                    "method": "GET",
                    "handler": handler,
                    "endpoint_kind": "mvc",
                    "source": "xml",
                    "view_name": "",
                    "session_writes": [],
                    "session_reads": [],
                    "error_flows": [],
                    "model_attributes": [],
                    "notification_hints": list(hints),
                    "http_security_roles": [],
                    "form_fields": [],
                    "validation_rules": [],
                    "async_endpoint": False,
                    "pagination": False,
                }
            )

    if not endpoints:
        return {}
    return {"endpoints": endpoints, "business_contexts": business_contexts}


def parse(file_path: Path, content: str) -> dict[str, Any]:  # noqa: PLR0912
    """Returns {} if file is not a Java HttpServlet or web.xml."""
    suffix = Path(file_path).suffix.lower()
    if suffix == ".xml":
        if not any(sig in content for sig in _WEB_XML_SIGNAL):
            return {}
        return _parse_web_xml(file_path, content)
    if suffix != ".java":
        return {}
    if not any(sig in content for sig in _SERVLET_SIGNAL):
        return {}

    endpoints: list[dict[str, Any]] = []
    form_fields: list[dict[str, Any]] = []
    nav_edges: list[dict[str, Any]] = []
    business_contexts: list[dict[str, Any]] = []

    # Collect URL patterns
    url_patterns: list[str] = []
    for m in _URL_PATTERN_RE.finditer(content):
        if m.group(1):
            url_patterns.extend(p.strip().strip("\"'") for p in m.group(1).split(","))
        elif m.group(2):
            url_patterns.append(m.group(2))
        elif m.group(3):
            url_patterns.append(m.group(3))

    if not url_patterns:
        # Fallback: use class name heuristic
        cls_m = re.search(r"class\s+(\w+)", content)
        if cls_m:
            url_patterns = ["/" + cls_m.group(1).replace("Servlet", "").lower()]

    # Collect request parameters from body
    req_params = list(dict.fromkeys(m.group(1) for m in _GET_PARAM_RE.finditer(content)))

    # Session reads/writes
    session_writes = list(dict.fromkeys(m.group(1) for m in _SESSION_SET_RE.finditer(content)))
    session_reads = list(dict.fromkeys(m.group(1) for m in _SESSION_GET_RE.finditer(content)))

    # Redirects → nav edges
    for m in _REDIRECT_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": url_patterns[0] if url_patterns else "",
                "to_screen": m.group(1),
                "kind": "redirect",
                "file": str(file_path),
                "line": 0,
            }
        )

    # Forwards → nav edges
    for m in _FORWARD_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": url_patterns[0] if url_patterns else "",
                "to_screen": m.group(1),
                "kind": "link",
                "file": str(file_path),
                "line": 0,
            }
        )

    # Build one endpoint per URL pattern per HTTP verb found in the body
    http_verbs = [_HTTP_VERB[m.group(1)] for m in _DO_METHOD_RE.finditer(content)]
    if not http_verbs:
        http_verbs = ["GET"]

    cls_m = re.search(r"class\s+(\w+)", content)
    handler = cls_m.group(1) if cls_m else str(Path(file_path).stem)

    for dm in _DO_METHOD_RE.finditer(content):
        method_line = content[: dm.start()].count("\n") + 1
        verb = _HTTP_VERB[dm.group(1)]
        for pattern in url_patterns:
            ep = {
                "path": pattern,
                "method": verb,
                "handler": handler,
                "file": str(file_path),
                "line": method_line,
            }
            endpoints.append(ep)
            business_contexts.append(
                {
                    "path": pattern,
                    "method": verb,
                    "handler": handler,
                    "endpoint_kind": "mvc",
                    "view_name": "",
                    "session_writes": session_writes,
                    "session_reads": session_reads,
                    "error_flows": [],
                    "model_attributes": [],
                    "notification_hints": [],
                    "http_security_roles": [],
                    "form_fields": [],
                    "validation_rules": [],
                    "async_endpoint": False,
                    "pagination": False,
                }
            )

    # Form fields from getParameter calls (POST methods)
    seen: set[str] = set()
    for param in req_params:
        if param in seen:
            continue
        seen.add(param)
        form_fields.append(
            {
                "endpoint_id": "",
                "name": param,
                "field_type": "text",
                "required": False,
                "constraints": (),
                "source": "annotation",
            }
        )

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if form_fields:
        result["form_fields"] = form_fields
    if nav_edges:
        result["nav_edges"] = nav_edges
    if business_contexts:
        result["business_contexts"] = business_contexts
    return result

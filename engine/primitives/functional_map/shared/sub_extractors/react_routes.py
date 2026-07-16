from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_REACT_SIGNALS = ("react-router", "from 'react-router", 'from "react-router', "<Route", "<Routes")

# <Route path="X" ... /> or <Route path="X">
_ROUTE_PATH_RE = re.compile(r'<Route\s[^>]*path=["\']([^"\']+)["\']', re.DOTALL)
# Element: element={<ComponentName>} or element={<ComponentName/>}
_ROUTE_ELEMENT_RE = re.compile(r"element=\{[^}]*?<(\w+)", re.DOTALL)
# PrivateRoute with role
_PRIVATE_ROUTE_RE = re.compile(
    r'<PrivateRoute\s[^>]*role=["\']([^"\']+)["\']',
    re.DOTALL,
)
# useNavigate / navigate() calls
_NAVIGATE_RE = re.compile(r'navigate\s*\(\s*["\']([^"\']+)["\']')
# Link to="X"
_LINK_TO_RE = re.compile(r'<Link\s[^>]*to=["\']([^"\']+)["\']')
# useLocation
_USE_LOCATION_RE = re.compile(r"useLocation\s*\(\s*\)")

# API calls: axios/fetch/api
_API_POST_RE = re.compile(r'(?:axios|fetch)\s*\.\s*post\s*\(\s*["\']([^"\']+)["\']')
_API_GET_RE = re.compile(r'(?:axios|fetch)\s*\.\s*get\s*\(\s*["\']([^"\']+)["\']')
_API_PUT_RE = re.compile(r'(?:axios|fetch)\s*\.\s*put\s*\(\s*["\']([^"\']+)["\']')
_API_DELETE_RE = re.compile(r'(?:axios|fetch)\s*\.\s*delete\s*\(\s*["\']([^"\']+)["\']')
_FETCH_RE = re.compile(
    r'fetch\s*\(\s*["\']([^"\']+)["\'](?:[^)]*method\s*:\s*["\']([A-Z]+)["\'])?',
    re.DOTALL,
)


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file has no React Router content."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".tsx", ".jsx", ".ts", ".js"):
        return {}
    if not any(sig in content for sig in _REACT_SIGNALS):
        return {}

    endpoints: list[dict[str, Any]] = []
    nav_edges: list[dict[str, Any]] = []
    guards: list[dict[str, Any]] = []

    # Extract routes
    route_paths = [m.group(1) for m in _ROUTE_PATH_RE.finditer(content)]
    route_elements = [m.group(1) for m in _ROUTE_ELEMENT_RE.finditer(content)]

    for i, path in enumerate(route_paths):
        if path in ("*",):
            continue
        element = route_elements[i] if i < len(route_elements) else ""
        endpoints.append(
            {
                "path": path,
                "method": "GET",
                "handler": element or path.strip("/").replace("/", "_") or "root",
                "file": str(file_path),
                "line": 0,
            }
        )

    # PrivateRoute guards
    for m in _PRIVATE_ROUTE_RE.finditer(content):
        role = m.group(1)
        guards.append(
            {
                "endpoint_id": "",
                "kind": "role_required",
                "roles": [role],
                "file": str(file_path),
                "line": 0,
            }
        )

    # Link-to nav edges
    for m in _LINK_TO_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": str(file_path.stem),
                "to_screen": m.group(1),
                "kind": "link",
                "file": str(file_path),
                "line": 0,
            }
        )

    # navigate() calls
    for m in _NAVIGATE_RE.finditer(content):
        nav_edges.append(
            {
                "from_screen": str(file_path.stem),
                "to_screen": m.group(1),
                "kind": "link",
                "file": str(file_path),
                "line": 0,
            }
        )

    # API endpoints discovered in component files
    for m in _API_POST_RE.finditer(content):
        endpoints.append(
            {
                "path": m.group(1),
                "method": "POST",
                "handler": str(file_path.stem),
                "file": str(file_path),
                "line": 0,
            }
        )
    for m in _API_GET_RE.finditer(content):
        endpoints.append(
            {
                "path": m.group(1),
                "method": "GET",
                "handler": str(file_path.stem),
                "file": str(file_path),
                "line": 0,
            }
        )
    for m in _API_PUT_RE.finditer(content):
        endpoints.append(
            {
                "path": m.group(1),
                "method": "PUT",
                "handler": str(file_path.stem),
                "file": str(file_path),
                "line": 0,
            }
        )
    for m in _API_DELETE_RE.finditer(content):
        endpoints.append(
            {
                "path": m.group(1),
                "method": "DELETE",
                "handler": str(file_path.stem),
                "file": str(file_path),
                "line": 0,
            }
        )

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if nav_edges:
        result["nav_edges"] = nav_edges
    if guards:
        result["guards"] = guards
    return result

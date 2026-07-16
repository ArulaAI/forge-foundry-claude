from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# JAX-RS annotations (Java/Jakarta EE)
_JAXRS_PATH_RE = re.compile(r'@Path\s*\(\s*"([^"]+)"\s*\)')
_JAXRS_METHOD_RE = re.compile(r"@(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b")
# MicroProfile / Quarkus REST annotations mirror JAX-RS; covered by same patterns.

# Spring Data REST
_REPO_REST_RE = re.compile(r'@RepositoryRestResource\s*\(\s*(?:path\s*=\s*)?"([^"]+)"')
_JPA_REPO_IFACE_RE = re.compile(r"interface\s+(\w+)\s+extends\s+\w*JpaRepository")

# Swagger/OpenAPI @ApiOperation
_API_OPERATION_VALUE_RE = re.compile(r'@ApiOperation\s*\([^)]*value\s*=\s*"([^"]+)"')
_API_OPERATION_NOTES_RE = re.compile(r'@ApiOperation\s*\([^)]*notes\s*=\s*"([^"]+)"')

# Flask/FastAPI/Bottle Python decorators
_FLASK_ROUTE_RE = re.compile(
    r'@(?:app|bp|router|api)\s*\.\s*(route|get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'
    r"(?:[^)]*methods\s*=\s*\[([^\]]+)\])?",
    re.DOTALL,
)
# FastAPI decorator style: @router.get("/path")
_FASTAPI_RE = re.compile(
    r'@(?:\w+)\s*\.\s*(get|post|put|delete|patch|head|options)\s*\(\s*["\']([^"\']+)["\']'
)
_HTTP_VERBS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")

# Express.js (Node) — router.get/post/put/delete('/path', ...)
_EXPRESS_ROUTE_RE = re.compile(
    r'(?:router|app)\s*\.\s*(get|post|put|delete|patch)\s*\(\s*["\']([^"\']+)["\']'
)

# Django URL conf: path('route/', view, name='...')
_DJANGO_PATH_RE = re.compile(
    r'(?:re_)?path\s*\(\s*r?["\']([^"\']+)["\'](?:[^,]*),\s*(\w+(?:\.\w+)*)'
)

# Rails routes: get '/path' => 'controller#action'
_RAILS_ROUTE_RE = re.compile(
    r"^\s*(get|post|put|delete|patch|resources?)\s+['\"]([^'\"]+)['\"]"
    r"(?:[^,\n]*(?:=>|,)\s*['\"](\w+#\w+)['\"])?",
    re.MULTILINE | re.IGNORECASE,
)

# Laravel routes: Route::get('/path', [...])
_LARAVEL_ROUTE_RE = re.compile(
    r"Route\s*::\s*(get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)['\"]"
)

# ASP.NET attribute routing: [Route("api/[controller]")] + [HttpGet]
_ASPNET_ROUTE_RE = re.compile(r'\[Route\s*\(\s*"([^"]+)"\s*\)\]')
_ASPNET_HTTP_RE = re.compile(r"\[(Http(?:Get|Post|Put|Delete|Patch))\]")

# Ruby on Rails-style resources
_RAILS_RESOURCE_RE = re.compile(
    r"^\s*resources?\s+:(\w+)",
    re.MULTILINE,
)


def _suffix(file_path: Path) -> str:
    return Path(file_path).suffix.lower()


def parse(file_path: Path, content: str) -> dict[str, Any]:  # noqa: PLR0912, PLR0915
    """Returns {} if the file has no REST API signals beyond what spring_mvc /
    servlet_controllers already cover."""
    suffix = _suffix(file_path)
    endpoints: list[dict[str, Any]] = []
    stem = Path(file_path).stem

    # ---- JAX-RS (Java/Kotlin) ----
    if suffix in (".java", ".kt") and "@Path" in content:
        class_path = ""
        lines = content.splitlines()
        for i, line in enumerate(lines):
            cp = _JAXRS_PATH_RE.search(line)
            if cp and i < 30:  # class-level @Path  # noqa: PLR2004
                class_path = cp.group(1).rstrip("/")
                continue
            mp = _JAXRS_METHOD_RE.search(line)
            if mp:
                method = mp.group(1)
                # Look at next 3 lines for @Path
                path_suffix = ""
                for lookahead in lines[i + 1 : i + 4]:
                    pp = _JAXRS_PATH_RE.search(lookahead)
                    if pp:
                        path_suffix = pp.group(1)
                        break
                full_path = class_path + ("/" + path_suffix.lstrip("/") if path_suffix else "")
                endpoints.append(
                    {
                        "path": full_path or "/",
                        "method": method,
                        "handler": stem,
                        "file": str(file_path),
                        "line": i + 1,
                    }
                )

    # ---- Python Flask/FastAPI ----
    elif suffix == ".py" and any(
        sig in content
        for sig in (
            "@app.route",
            "@router.",
            "@bp.",
            "@app.get",
            "@app.post",
            "@app.put",
            "@app.patch",
            "@app.delete",
            "@app.head",
            "@app.options",
        )
    ):
        for m in _FLASK_ROUTE_RE.finditer(content):
            verb = m.group(1).upper()
            path = m.group(2)
            methods_str = m.group(3) or ""
            methods = (
                [v.strip().strip("'\"") for v in methods_str.split(",")] if methods_str else [verb]
            )
            lineno: int = content[: m.start()].count("\n") + 1
            for method in methods:
                endpoints.append(
                    {
                        "path": path,
                        "method": method.upper() if method.upper() in _HTTP_VERBS else "GET",
                        "handler": stem,
                        "file": str(file_path),
                        "line": lineno,
                    }
                )
        for m in _FASTAPI_RE.finditer(content):
            method = m.group(1).upper()
            path = m.group(2)
            lineno = content[: m.start()].count("\n") + 1
            endpoints.append(
                {
                    "path": path,
                    "method": method,
                    "handler": stem,
                    "file": str(file_path),
                    "line": lineno,
                }
            )
        if endpoints:
            seen = set()
            deduped = []
            for ep in endpoints:
                key = (ep["path"], ep["method"])
                if key not in seen:
                    seen.add(key)
                    deduped.append(ep)
            endpoints = deduped

    # ---- Express.js (Node) ----
    elif suffix in (".js", ".ts") and any(
        sig in content for sig in ("router.get", "router.post", "app.get", "app.post")
    ):
        for m in _EXPRESS_ROUTE_RE.finditer(content):
            endpoints.append(
                {
                    "path": m.group(2),
                    "method": m.group(1).upper(),
                    "handler": stem,
                    "file": str(file_path),
                    "line": content[: m.start()].count("\n") + 1,
                }
            )

    # ---- ASP.NET (C#) ----
    elif suffix in (".cs",) and "[Route(" in content:
        base_path = ""
        rm = _ASPNET_ROUTE_RE.search(content)
        if rm:
            base_path = rm.group(1)
        for m in _ASPNET_HTTP_RE.finditer(content):
            http_method = m.group(1).replace("Http", "").upper()
            endpoints.append(
                {
                    "path": base_path or "/",
                    "method": http_method,
                    "handler": stem,
                    "file": str(file_path),
                    "line": content[: m.start()].count("\n") + 1,
                }
            )

    # ---- Rails routes.rb ----
    elif suffix == ".rb" and ("routes.draw" in content or "resources" in content):
        for m in _RAILS_ROUTE_RE.finditer(content):
            endpoints.append(
                {
                    "path": m.group(2),
                    "method": m.group(1).upper(),
                    "handler": m.group(3) or stem,
                    "file": str(file_path),
                    "line": content[: m.start()].count("\n") + 1,
                }
            )

    # ---- Laravel (PHP) ----
    elif suffix == ".php" and "Route::" in content:
        for m in _LARAVEL_ROUTE_RE.finditer(content):
            endpoints.append(
                {
                    "path": m.group(2),
                    "method": m.group(1).upper(),
                    "handler": stem,
                    "file": str(file_path),
                    "line": content[: m.start()].count("\n") + 1,
                }
            )

    # ---- @RepositoryRestResource (Spring Data REST) ----
    business_contexts: list[dict[str, Any]] = []
    _is_java = suffix in (".java", ".kt")
    if _is_java and "@RepositoryRestResource" in content and "JpaRepository" in content:
        repo_m = _REPO_REST_RE.search(content)
        iface_m = _JPA_REPO_IFACE_RE.search(content)
        if repo_m and iface_m:
            rest_path = "/" + repo_m.group(1).lstrip("/")
            repo_name = iface_m.group(1)
            repo_lineno: int = content[: repo_m.start()].count("\n") + 1
            crud = [
                ("GET", rest_path),
                ("GET", rest_path + "/{id}"),
                ("POST", rest_path),
                ("PUT", rest_path + "/{id}"),
                ("PATCH", rest_path + "/{id}"),
                ("DELETE", rest_path + "/{id}"),
            ]
            for method, path in crud:
                endpoints.append(
                    {
                        "path": path,
                        "method": method,
                        "handler": repo_name,
                        "file": str(file_path),
                        "line": repo_lineno,
                    }
                )

    # ---- @ApiOperation → api_description on business_contexts ----
    # Lookahead regexes for the adjacent mapping annotation line
    _MAPPING_PATH_RE = re.compile(
        r'@(?:Get|Post|Put|Delete|Patch|Request)Mapping\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']'
    )
    _MAPPING_METHOD_RE = re.compile(r"@(Get|Post|Put|Delete|Patch)Mapping\b", re.IGNORECASE)
    _MAPPING_HANDLER_RE = re.compile(r"(?:Mapping|@RequestMapping)\([^)]*\)\s+\w+\s+\w+\s+(\w+)\(")
    _HTTP_VERB_MAP = {
        "get": "GET",
        "post": "POST",
        "put": "PUT",
        "delete": "DELETE",
        "patch": "PATCH",
        "request": "",
    }
    if suffix in (".java", ".kt") and "@ApiOperation" in content:
        lines = content.splitlines()
        for i, line in enumerate(lines):
            val_m = _API_OPERATION_VALUE_RE.search(line)
            if not val_m:
                continue
            description = val_m.group(1)
            notes_m = _API_OPERATION_NOTES_RE.search(line)
            if notes_m:
                description = description + " — " + notes_m.group(1)
            # Scan adjacent lines for the mapping annotation to extract path, method, handler
            handler = stem
            ep_path = ""
            ep_method = ""
            for lookahead in lines[i + 1 : i + 4]:
                ph = _MAPPING_PATH_RE.search(lookahead)
                if ph:
                    ep_path = ph.group(1)
                vm = _MAPPING_METHOD_RE.search(lookahead)
                if vm:
                    ep_method = _HTTP_VERB_MAP.get(vm.group(1).lower(), "")
                mh = _MAPPING_HANDLER_RE.search(lookahead)
                if mh:
                    handler = mh.group(1)
                if ep_path:
                    break
            bc: dict[str, object] = {
                "handler": handler,
                "api_description": description,
                "file": str(file_path),
                "line": i + 1,
            }
            if ep_path:
                bc["path"] = ep_path
            if ep_method:
                bc["method"] = ep_method
            business_contexts.append(bc)

    if not endpoints and not business_contexts:
        return {}

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if business_contexts:
        result["business_contexts"] = business_contexts
    return result

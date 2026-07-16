from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# OpenAPI / Swagger / API Gateway signals
_OPENAPI_SIGNALS = ("openapi:", "swagger:", '"openapi":', '"swagger":')
# AWS API Gateway signals
_APIGW_SIGNALS = ("x-amazon-apigateway", "httpApi:", "restApi:")

# YAML path key: simple "/path:" pattern
_YAML_PATH_RE = re.compile(r"^\s{2}(/[^\s:]+)\s*:", re.MULTILINE)
# HTTP method under a path block
_YAML_METHOD_RE = re.compile(
    r"^\s{4}(get|post|put|patch|delete|head|options)\s*:", re.MULTILINE | re.IGNORECASE
)
# operationId
_OPERATION_ID_RE = re.compile(r"operationId\s*:\s*(\S+)")
# security schemes
_SECURITY_RE = re.compile(r"^\s{6}security\s*:", re.MULTILINE)
# x-amazon-apigateway-authorizer
_APIGW_AUTH_RE = re.compile(r"x-amazon-apigateway-authorizer")


def _parse_openapi_text(content: str, file_path: Path) -> dict[str, Any]:
    """Regex-based OpenAPI path/method extraction (no YAML parsing needed)."""
    endpoints: list[dict[str, Any]] = []
    guards: list[dict[str, Any]] = []

    # Find paths block
    paths_start = content.find("\npaths:")
    if paths_start == -1:
        paths_start = content.find('"paths"')
    if paths_start == -1:
        return {}

    paths_section = content[paths_start:]

    # Find each path key
    for path_m in _YAML_PATH_RE.finditer(paths_section):
        path = path_m.group(1)
        path_start = path_m.end()
        # Look for methods within the next 1000 chars (one path block)
        path_end = path_start + 1000
        block = paths_section[path_start:path_end]
        for method_m in _YAML_METHOD_RE.finditer(block):
            method = method_m.group(1).upper()
            op_id_m = _OPERATION_ID_RE.search(block[method_m.start() : method_m.start() + 200])
            handler = op_id_m.group(1) if op_id_m else f"{method.lower()}{path.replace('/', '_')}"
            line = content[: paths_start + path_m.start()].count("\n") + 1
            endpoints.append(
                {
                    "path": path,
                    "method": method,
                    "handler": handler,
                    "file": str(file_path),
                    "line": line,
                }
            )
            # Check for security on this operation
            op_block = block[method_m.start() : method_m.start() + 300]
            if _SECURITY_RE.search(op_block) or _APIGW_AUTH_RE.search(op_block):
                guards.append(
                    {
                        "endpoint_id": handler,
                        "kind": "auth_required",
                        "roles": [],
                        "file": str(file_path),
                        "line": line,
                    }
                )

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if guards:
        result["guards"] = guards
    return result


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file is not an OpenAPI/Swagger/API Gateway spec."""
    suffix = Path(file_path).suffix.lower()
    name = Path(file_path).name.lower()

    is_yaml = suffix in (".yaml", ".yml")
    is_json = suffix == ".json"

    if not (is_yaml or is_json):
        return {}

    has_openapi = any(sig in content for sig in _OPENAPI_SIGNALS)
    has_apigw = any(sig in content for sig in _APIGW_SIGNALS)

    if not (has_openapi or has_apigw):
        # Check filename heuristics
        if not any(kw in name for kw in ("openapi", "swagger", "api-spec", "apigw", "api_gateway")):
            return {}
        if not (has_openapi or has_apigw):
            return {}

    if is_json:
        # Try to parse JSON for path keys
        try:
            spec = json.loads(content)
            endpoints: list[dict[str, Any]] = []
            guards: list[dict[str, Any]] = []
            for path, path_obj in spec.get("paths", {}).items():
                for method, op in path_obj.items():
                    _valid = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
                    if method.upper() not in _valid:
                        continue
                    if not isinstance(op, dict):
                        continue
                    handler = op.get("operationId") or f"{method}{path.replace('/', '_')}"
                    endpoints.append(
                        {
                            "path": path,
                            "method": method.upper(),
                            "handler": handler,
                            "file": str(file_path),
                            "line": 0,
                        }
                    )
                    if op.get("security") or "x-amazon-apigateway-authorizer" in str(op):
                        guards.append(
                            {
                                "endpoint_id": handler,
                                "kind": "auth_required",
                                "roles": [],
                                "file": str(file_path),
                                "line": 0,
                            }
                        )
            result: dict[str, Any] = {}
            if endpoints:
                result["endpoints"] = endpoints
            if guards:
                result["guards"] = guards
            return result
        except (json.JSONDecodeError, ValueError):
            return {}

    # YAML: regex-based (avoids yaml import dependency)
    return _parse_openapi_text(content, file_path)

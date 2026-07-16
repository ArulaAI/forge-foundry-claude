from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# gRPC .proto signals
_PROTO_SIGNALS = ("service ", "rpc ", "message ", 'syntax = "proto')

# service Name { rpc ... }
_SERVICE_BLOCK_RE = re.compile(r"service\s+(\w+)\s*\{([^}]+)\}", re.DOTALL)
# rpc MethodName (RequestType) returns (ResponseType)
_RPC_RE = re.compile(
    r"rpc\s+(\w+)\s*\(\s*(?:stream\s+)?(\w+)\s*\)\s*returns\s*\(\s*(?:stream\s+)?(\w+)\s*\)"
)
# message Name { fields }
_MESSAGE_BLOCK_RE = re.compile(r"message\s+(\w+)\s*\{([^}]+)\}", re.DOTALL)
# field declaration: [repeated] TypeName fieldName = N;
_PROTO_FIELD_RE = re.compile(
    r"^\s*(?:repeated\s+)?(\w+)\s+(\w+)\s*=\s*\d+\s*;",
    re.MULTILINE,
)
# option (google.api.http)
_HTTP_RULE_RE = re.compile(
    r'option\s*\(google\.api\.http\)\s*=\s*\{[^}]*(?:get|post|put|patch|delete)\s*:\s*"([^"]+)"'
)


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file is not a .proto gRPC schema."""
    suffix = Path(file_path).suffix.lower()
    if suffix != ".proto":
        return {}
    if not any(sig in content for sig in _PROTO_SIGNALS):
        return {}

    endpoints: list[dict[str, Any]] = []
    form_fields: list[dict[str, Any]] = []

    # HTTP rule mapping for REST transcoding
    http_rules: dict[str, str] = {}
    for m in _HTTP_RULE_RE.finditer(content):
        http_rules[m.group(0)] = m.group(1)

    # Services → endpoints
    for svc_m in _SERVICE_BLOCK_RE.finditer(content):
        svc_name = svc_m.group(1)
        svc_body = svc_m.group(2)
        line_no = content[: svc_m.start()].count("\n") + 1
        for rpc_m in _RPC_RE.finditer(svc_body):
            method_name = rpc_m.group(1)
            endpoints.append(
                {
                    "path": f"/{svc_name}/{method_name}",
                    "method": "RPC",
                    "handler": f"{svc_name}.{method_name}",
                    "file": str(file_path),
                    "line": line_no,
                }
            )

    # Messages → form fields (treat as input type schemas)
    for msg_m in _MESSAGE_BLOCK_RE.finditer(content):
        msg_name = msg_m.group(1)
        msg_body = msg_m.group(2)
        # Skip response-type messages (heuristic: name ends in Response)
        if msg_name.endswith("Response") or msg_name.endswith("Reply"):
            continue
        order = 0
        for field_m in _PROTO_FIELD_RE.finditer(msg_body):
            field_name = field_m.group(2)
            if field_name in ("reserved",):
                continue
            order += 1
            form_fields.append(
                {
                    "endpoint_id": msg_name,
                    "name": field_name,
                    "field_type": "text",
                    "required": False,
                    "constraints": (),
                    "source": "proto",
                    "display_order": order,
                }
            )

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if form_fields:
        result["form_fields"] = form_fields
    return result

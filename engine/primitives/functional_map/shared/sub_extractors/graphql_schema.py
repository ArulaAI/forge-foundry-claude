from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# GraphQL SDL signals
_GQL_SIGNALS = ("type Query", "type Mutation", "type Subscription", "schema {", "extend type")

# type Name { ... } — captures the block including newlines
_TYPE_BLOCK_RE = re.compile(
    r"(?:^|\n)\s*(type|input|interface|extend\s+type)\s+(\w+)[^{]*\{([^}]+)\}",
    re.DOTALL,
)
# Field line: fieldName(args): ReturnType
_FIELD_RE = re.compile(r"^\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*([\w!\[\]]+)", re.MULTILINE)
# @deprecated directive
_DEPRECATED_RE = re.compile(r"@deprecated")
# @auth / @hasRole directive
_AUTH_RE = re.compile(r"@(?:auth|hasRole|authorized|isAuthenticated)")


def parse(file_path: Path, content: str) -> dict[str, Any]:
    """Returns {} if file is not a GraphQL schema file."""
    suffix = Path(file_path).suffix.lower()
    if suffix not in (".graphql", ".gql", ".graphqls") and (
        suffix not in (".ts", ".js") or not any(sig in content for sig in _GQL_SIGNALS)
    ):
        return {}
    if not any(sig in content for sig in _GQL_SIGNALS):
        return {}

    endpoints: list[dict[str, Any]] = []
    form_fields: list[dict[str, Any]] = []
    guards: list[dict[str, Any]] = []

    for type_m in _TYPE_BLOCK_RE.finditer(content):
        kind = type_m.group(1).strip()
        type_name = type_m.group(2)
        body = type_m.group(3)
        line_no = content[: type_m.start()].count("\n") + 1

        if (
            type_name in ("Query", "Mutation", "Subscription")
            or "Query" in type_name
            or "Mutation" in type_name
        ):
            # Operation type — each field is an endpoint
            for field_m in _FIELD_RE.finditer(body):
                field_name = field_m.group(1)
                return_type = field_m.group(2)
                # Skip __typename etc.
                if field_name.startswith("_"):
                    continue
                method = "QUERY"
                if type_name == "Mutation" or "Mutation" in type_name:
                    method = "MUTATION"
                elif type_name == "Subscription" or "Subscription" in type_name:
                    method = "SUBSCRIPTION"
                ep: dict[str, Any] = {
                    "path": f"/{field_name}",
                    "method": method,
                    "handler": f"{type_name}.{field_name}",
                    "file": str(file_path),
                    "line": line_no,
                }
                if _AUTH_RE.search(body):
                    guards.append(
                        {
                            "endpoint_id": f"{type_name}.{field_name}",
                            "kind": "graphql_auth",
                            "roles": [],
                            "file": str(file_path),
                            "line": line_no,
                        }
                    )
                endpoints.append(ep)
        elif kind in ("input", "Input") or type_name.endswith("Input"):
            # Input type → form fields
            order = 0
            for field_m in _FIELD_RE.finditer(body):
                field_name = field_m.group(1)
                return_type = field_m.group(2)
                if field_name.startswith("_"):
                    continue
                required = "!" in return_type
                order += 1
                form_fields.append(
                    {
                        "endpoint_id": type_name,
                        "name": field_name,
                        "field_type": "text",
                        "required": required,
                        "constraints": (),
                        "source": "graphql",
                        "display_order": order,
                    }
                )

    result: dict[str, Any] = {}
    if endpoints:
        result["endpoints"] = endpoints
    if form_fields:
        result["form_fields"] = form_fields
    if guards:
        result["guards"] = guards
    return result

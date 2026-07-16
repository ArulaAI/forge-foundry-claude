"""S40 /api-contract-review — OpenAPI 3.0 / 3.1 parser.

Parses YAML or JSON files with an `openapi:` key.
Extracts path/method pairs as Endpoint objects with file:line information.

Supports:
  - OpenAPI 3.0.x (openapi: "3.0.x")
  - OpenAPI 3.1.x (openapi: "3.1.x")

**SP-F (#1386) — additive schema extraction.** :func:`extract_operations`
produces an :class:`ApiExtraction` (request/response DTOs + per-field
validation constraints + security requirements) from the SAME parsed document,
reading only what the contract DECLARES (no source-code inference). This is
independent of and additive to the review path; the review's :func:`parse_openapi`
entry point is unchanged.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml

from primitives.api_contract_review.shared.models import (
    ApiExtraction,
    ContractFormat,
    Endpoint,
    ExtractedOperation,
    FieldConstraint,
    ResponseShape,
    SecurityScheme,
)

if TYPE_CHECKING:
    from pathlib import Path

# HTTP methods recognized in OpenAPI path items
_OPENAPI_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})


class OpenAPIParseError(Exception):
    """Raised when an OpenAPI file cannot be parsed."""


def _detect_format(version_str: str) -> ContractFormat:
    """Map an openapi version string to ContractFormat."""
    if str(version_str).startswith("3.0"):
        return "openapi-3.0"
    if str(version_str).startswith("3.1"):
        return "openapi-3.1"
    raise OpenAPIParseError(f"Unsupported OpenAPI version {version_str!r}; expected 3.0.x or 3.1.x")


def _load_document(file_path: Path) -> tuple[dict[str, Any], int]:
    """Load a YAML or JSON file; return (data, line_offset).

    line_offset is 1 for YAML (1-indexed lines) and 1 for JSON.

    Raises:
        OpenAPIParseError: If the file cannot be parsed.
    """
    text = file_path.read_text(encoding="utf-8")
    suffix = file_path.suffix.lower()
    try:
        if suffix == ".json":
            data: dict[str, Any] = json.loads(text)
        else:
            data = yaml.safe_load(text) or {}
    except (yaml.YAMLError, json.JSONDecodeError) as exc:
        raise OpenAPIParseError(f"Failed to parse {file_path}: {exc}") from exc
    return data, 1


def _find_line_for_path(text: str, path_segment: str) -> int:
    """Heuristically find the 1-indexed line number where *path_segment* appears."""
    for i, line in enumerate(text.splitlines(), 1):
        if path_segment in line:
            return i
    return 1


def parse_openapi(file_path: Path) -> tuple[list[Endpoint], ContractFormat]:
    """Parse an OpenAPI 3.0/3.1 contract file and extract endpoints.

    Args:
        file_path: Path to the OpenAPI YAML or JSON file.

    Returns:
        (endpoints, format) where format is "openapi-3.0" or "openapi-3.1".

    Raises:
        OpenAPIParseError: If the file lacks an `openapi:` key or has a bad version.
    """
    text = file_path.read_text(encoding="utf-8")
    data, _ = _load_document(file_path)

    if "openapi" not in data:
        raise OpenAPIParseError(f"{file_path} does not have an 'openapi' key")

    contract_format = _detect_format(data["openapi"])
    file_str = str(file_path)

    endpoints: list[Endpoint] = []
    paths = data.get("paths") or {}

    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        path_line = _find_line_for_path(text, str(path_str))
        for method, operation in path_item.items():
            if method.lower() not in _OPENAPI_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            endpoints.append(
                Endpoint(
                    path=str(path_str),
                    method_or_op=method.upper(),
                    format=contract_format,
                    file=file_str,
                    line=path_line,
                )
            )

    return endpoints, contract_format


# ---------------------------------------------------------------------------
# SP-F (#1386) — schema extraction (additive; independent of the review path)
# ---------------------------------------------------------------------------


def _ref_name(ref: str) -> str:
    """The trailing component of a ``$ref`` pointer (e.g. ``#/.../Owner`` → ``Owner``)."""
    return ref.rsplit("/", 1)[-1] if ref else ""


def _resolve_schema(schema: Any, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Resolve a schema node, following a single ``$ref`` into ``components.schemas``.

    Returns ``(resolved_schema_dict, schema_name)``. ``schema_name`` is the
    ``$ref`` component name when the node was a reference (or a named component),
    else ``""``. Only same-document component ``$ref``\\ s are followed — external
    refs are left unresolved (their name is still reported). Non-dict inputs
    resolve to ``({}, "")``.
    """
    if not isinstance(schema, dict):
        return {}, ""
    ref = schema.get("$ref")
    if isinstance(ref, str):
        name = _ref_name(ref)
        components = (data.get("components") or {}).get("schemas") or {}
        target = components.get(name)
        if isinstance(target, dict):
            return target, name
        return {}, name
    return schema, ""


def _unwrap_media_schema(content: Any, data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Pull the schema out of a ``content`` block (first JSON-ish media type).

    Prefers ``application/json``; falls back to the first media type declared.
    Unwraps a top-level ``type: array`` to its ``items`` schema so the field list
    describes the element DTO (the row shape) rather than the array wrapper.
    """
    if not isinstance(content, dict) or not content:
        return {}, ""
    media = content.get("application/json")
    if not isinstance(media, dict):
        # first declared media type
        media = next((v for v in content.values() if isinstance(v, dict)), {})
    schema = media.get("schema") if isinstance(media, dict) else None
    resolved, name = _resolve_schema(schema, data)
    if resolved.get("type") == "array" and isinstance(resolved.get("items"), dict):
        item_resolved, item_name = _resolve_schema(resolved["items"], data)
        return item_resolved, item_name or name
    return resolved, name


def _field_constraints(schema: dict[str, Any], data: dict[str, Any]) -> list[FieldConstraint]:
    """Extract per-field validation constraints from an object schema's ``properties``.

    Reads only declared constraints — ``required`` (from the schema's ``required``
    list), ``format``, ``minimum``/``maximum``, ``minLength``/``maxLength``,
    ``pattern``, and ``enum``. Fields are returned in declaration order. Nested
    objects/refs are named by type but not recursed (the DTO name carries the link).
    """
    resolved, _ = _resolve_schema(schema, data)
    props = resolved.get("properties")
    if not isinstance(props, dict):
        return []
    required_names = set(resolved.get("required") or [])
    fields: list[FieldConstraint] = []
    for fname, fschema in props.items():
        fresolved, fref_name = _resolve_schema(fschema, data)
        raw_type = fresolved.get("type")
        ftype = str(raw_type) if raw_type is not None else (fref_name or "")
        enum_vals = fresolved.get("enum")
        fields.append(
            FieldConstraint(
                field=str(fname),
                type=ftype,
                required=str(fname) in required_names,
                format=fresolved.get("format"),
                minimum=fresolved.get("minimum"),
                maximum=fresolved.get("maximum"),
                min_length=fresolved.get("minLength"),
                max_length=fresolved.get("maxLength"),
                pattern=fresolved.get("pattern"),
                enum=[str(v) for v in enum_vals] if isinstance(enum_vals, list) else [],
            )
        )
    return fields


def _operation_security(operation: dict[str, Any], doc_security: list[Any]) -> list[str]:
    """The security-requirement scheme names for one operation.

    Per-operation ``security`` overrides the document default when present (an
    empty list means "explicitly no auth"). Each requirement is a mapping of
    scheme-name → scopes; we surface the scheme names in declaration order.
    """
    sec = operation.get("security")
    requirements = sec if isinstance(sec, list) else doc_security
    names: list[str] = []
    for req in requirements or []:
        if isinstance(req, dict):
            for scheme_name in req:
                if scheme_name not in names:
                    names.append(str(scheme_name))
    return names


def _security_schemes(data: dict[str, Any]) -> list[SecurityScheme]:
    """The declared ``components.securitySchemes`` as :class:`SecurityScheme` records."""
    schemes = (data.get("components") or {}).get("securitySchemes") or {}
    out: list[SecurityScheme] = []
    if not isinstance(schemes, dict):
        return out
    for name, spec in schemes.items():
        if not isinstance(spec, dict):
            continue
        out.append(
            SecurityScheme(
                name=str(name),
                type=str(spec.get("type") or ""),
                scheme=str(spec.get("scheme") or ""),
                bearer_format=str(spec.get("bearerFormat") or ""),
                location=str(spec.get("in") or ""),
            )
        )
    return out


def _extract_operation(
    path_str: str,
    method: str,
    operation: dict[str, Any],
    data: dict[str, Any],
    doc_security: list[Any],
) -> ExtractedOperation:
    """Build one :class:`ExtractedOperation` from a resolved path-item operation."""
    request_body = operation.get("requestBody")
    req_fields: list[FieldConstraint] = []
    req_name = ""
    if isinstance(request_body, dict):
        req_schema, req_name = _unwrap_media_schema(request_body.get("content"), data)
        req_fields = _field_constraints(req_schema, data)

    responses: list[ResponseShape] = []
    resp_block = operation.get("responses")
    if isinstance(resp_block, dict):
        for status_code, resp in resp_block.items():
            if not isinstance(resp, dict):
                continue
            resp_schema, resp_name = _unwrap_media_schema(resp.get("content"), data)
            responses.append(
                ResponseShape(
                    status_code=str(status_code),
                    description=str(resp.get("description") or ""),
                    schema_name=resp_name,
                    fields=_field_constraints(resp_schema, data),
                )
            )

    return ExtractedOperation(
        method=method.upper(),
        path=str(path_str),
        operation_id=str(operation.get("operationId") or ""),
        summary=str(operation.get("summary") or ""),
        request_schema_name=req_name,
        request_fields=req_fields,
        responses=responses,
        security=_operation_security(operation, doc_security),
    )


def extract_operations(file_path: Path) -> ApiExtraction:
    """Extract the declared REST-API schema from an OpenAPI 3.0/3.1 contract.

    Additive to the review path (SP-F, #1386). Reads only what the contract
    DECLARES — per operation: the request DTO fields, per-status response DTO
    fields, per-field validation constraints, and the security requirement names —
    plus the document's ``securitySchemes``. No source-code inference; deterministic.

    Args:
        file_path: Path to the OpenAPI YAML or JSON file.

    Returns:
        An :class:`ApiExtraction` (empty ``operations`` when the document declares
        no paths).

    Raises:
        OpenAPIParseError: If the file lacks an ``openapi:`` key or has a bad version.
    """
    data, _ = _load_document(file_path)
    if "openapi" not in data:
        raise OpenAPIParseError(f"{file_path} does not have an 'openapi' key")
    contract_format = _detect_format(data["openapi"])

    raw_doc_security = data.get("security")
    doc_security: list[Any] = raw_doc_security if isinstance(raw_doc_security, list) else []

    operations: list[ExtractedOperation] = []
    paths = data.get("paths") or {}
    for path_str, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method.lower() not in _OPENAPI_METHODS:
                continue
            if not isinstance(operation, dict):
                continue
            operations.append(
                _extract_operation(str(path_str), method, operation, data, doc_security)
            )

    return ApiExtraction(
        contract_path=str(file_path),
        format=contract_format,
        operations=operations,
        security_schemes=_security_schemes(data),
    )

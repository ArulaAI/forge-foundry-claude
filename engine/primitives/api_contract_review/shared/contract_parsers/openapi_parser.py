"""S40 /api-contract-review — OpenAPI 3.0 / 3.1 parser.

Parses YAML or JSON files with an `openapi:` key.
Extracts path/method pairs as Endpoint objects with file:line information.

Supports:
  - OpenAPI 3.0.x (openapi: "3.0.x")
  - OpenAPI 3.1.x (openapi: "3.1.x")
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import yaml

from primitives.api_contract_review.shared.models import ContractFormat, Endpoint

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
